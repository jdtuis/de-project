"""BR-04: Data Quality & Consistency Control.

Kiểm tra chất lượng dữ liệu trực tiếp trên warehouse (database dwh). Quy tắc được sinh tự động từ cấu trúc bảng:
  - Missing key      : khóa ngoại thiếu (NULL hoặc <= 0, ví dụ chuyến chưa gán xe hoặc tài xế), mã tự nhiên rỗng
  - Referential      : khóa ngoại không tồn tại ở bảng dim (orphan)
  - Uniqueness       : trùng khóa chính hoặc trùng grain
  - Invalid value    : số đo âm (dặm, gallons, doanh thu...)
  - Missing value    : số đo bị NULL
  - Format           : cột boolean ngoài {0,1}, ngày ngoài khoảng 2000-01-01 .. hôm nay + 1 năm
  - Consistency      : total_cost khác gallons x giá, total_revenue khác tổng các thành phần
Mỗi quy tắc quét toàn bộ dòng của bảng và cho kết quả PASS/FAIL.
"""

import re
import uuid
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from common import get_client, get_creds, run_query, run_query_slow

st.set_page_config(page_title="Data quality", layout="wide")
creds = get_creds()
DB = creds[3]


def qi(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def tbl(name: str) -> str:
    return f"{qi(DB)}.{qi(name)}"


st.title("Data quality")
st.caption(
    "Kiểm tra dữ liệu đã nằm trong warehouse: khóa thiếu, khóa mồ côi, trùng lặp, giá trị và định dạng không hợp lệ."
)

# ---------------------------------------------------------------------------
# Đọc cấu trúc warehouse
# ---------------------------------------------------------------------------
try:
    meta = run_query_slow(
        "SELECT table, name, type FROM system.columns "
        f"WHERE database = '{DB.replace(chr(39), '')}' ORDER BY table, position",
        creds,
    )
except Exception as e:
    st.error("Không đọc được cấu trúc database.")
    st.code(str(e))
    st.stop()

schema = {t: list(zip(g["name"], g["type"])) for t, g in meta.groupby("table")}
names_of = {t: [c for c, _ in cols] for t, cols in schema.items()}
all_tables = set(schema)

facts = sorted(t for t in all_tables if t.startswith("v_fact_"))
facts += sorted(
    t for t in all_tables
    if t.startswith("fact_") and not t.endswith("_streaming") and f"v_{t}" not in all_tables
)
dims = sorted(t for t in all_tables if t.startswith("dim_"))


def base_type(t: str) -> str:
    prev = None
    while prev != t:
        prev = t
        t = re.sub(r"^(Nullable|LowCardinality)\((.*)\)$", r"\2", t)
    return t


def is_num(t: str) -> bool:
    return base_type(t).startswith(("Int", "UInt", "Float", "Decimal"))


def is_date(t: str) -> bool:
    return base_type(t).startswith("Date")


MEASURE = ("miles", "gallons", "revenue", "cost", "price", "weight", "hours", "charges", "surcharge", "pieces", "duration")


def fk_target(col: str, self_table: str):
    """Tìm bảng được trỏ tới bởi cột khóa `col`. Trả về (bảng, cột) hoặc None nếu đó là khóa chính."""
    if col.endswith("date_key") and "dim_date" in all_tables and "date_key" in names_of["dim_date"]:
        return "dim_date", "date_key"
    base = col[:-4]
    d = f"dim_{base}"
    if d in all_tables and col in names_of[d]:
        return d, col
    for cand in (f"v_fact_{base}", f"fact_{base}"):
        if cand in all_tables and cand != self_table and col in names_of[cand]:
            return cand, col
    return None


def build_rules() -> list:
    """Mỗi quy tắc: dict(table, category, rule, column, fail_expr, sample_sql)."""
    rules = []

    def add(table, category, rule, column, fail_expr, sample_where=None, sample_sql=None):
        if sample_sql is None and sample_where:
            sample_sql = f"SELECT * FROM {tbl(table)} WHERE {sample_where} LIMIT 50"
        rules.append(dict(table=table, category=category, rule=rule, column=column,
                          fail_expr=fail_expr, sample_sql=sample_sql))

    def pred(table, category, rule, column, p):
        add(table, category, rule, column, f"countIf(ifNull({p}, 0))", sample_where=p)

    for t in facts:
        cols = schema[t]
        fk_cols, pk_cols = [], []
        for c, ty in cols:
            if c.endswith("_key"):
                target = fk_target(c, t)
                (fk_cols if target else pk_cols).append((c, target))

        for c, (tt, tc) in fk_cols:
            q = qi(c)
            pred(t, "Missing key", f"{c} chưa được gán (NULL hoặc <= 0)", c, f"({q} IS NULL OR {q} <= 0)")
            pred(t, "Referential integrity", f"{c} không tồn tại trong {tt}", c,
                 f"({q} > 0 AND {q} NOT IN (SELECT {qi(tc)} FROM {tbl(tt)}))")

        # Duy nhất: khóa chính, mã tự nhiên cùng gốc; nếu không có khóa chính thì kiểm tra grain
        if pk_cols:
            pk = pk_cols[0][0]
            sk = qi(pk)
            add(t, "Uniqueness", f"{pk} không trùng", pk, f"count() - uniqExact({sk})",
                sample_sql=f"SELECT * FROM {tbl(t)} WHERE {sk} IN (SELECT {sk} FROM {tbl(t)} GROUP BY {sk} HAVING count() > 1) LIMIT 50")
            nat = pk[:-4] + "_id"
            if nat in names_of[t]:
                nq = qi(nat)
                add(t, "Uniqueness", f"{nat} không trùng", nat, f"count() - uniqExact({nq})",
                    sample_sql=f"SELECT * FROM {tbl(t)} WHERE {nq} IN (SELECT {nq} FROM {tbl(t)} GROUP BY {nq} HAVING count() > 1) LIMIT 50")
        elif fk_cols:
            keys = ", ".join(qi(c) for c, _ in fk_cols)
            add(t, "Uniqueness", f"({', '.join(c for c, _ in fk_cols)}) không trùng", ", ".join(c for c, _ in fk_cols),
                f"count() - uniqExact({keys})",
                sample_sql=f"SELECT * FROM {tbl(t)} WHERE ({keys}) IN (SELECT {keys} FROM {tbl(t)} GROUP BY {keys} HAVING count() > 1) LIMIT 50")

        for c, ty in cols:
            q = qi(c)
            bt = base_type(ty)
            if bt == "String" and c.endswith("_id"):
                pred(t, "Missing key", f"{c} rỗng", c, f"{q} = ''")
            lname = c.lower()
            if is_num(ty) and any(k in lname for k in MEASURE):
                if ty.startswith("Nullable"):
                    pred(t, "Missing value", f"{c} bị NULL", c, f"{q} IS NULL")
                pred(t, "Invalid value", f"{c} không âm", c, f"{q} < 0")
            if bt.startswith("UInt8") and (lname.startswith("is_") or lname.endswith("_flag")):
                pred(t, "Format", f"{c} chỉ nhận 0 hoặc 1", c, f"{q} NOT IN (0, 1)")
            if is_date(ty):
                pred(t, "Format", f"{c} nằm trong 2000-01-01 .. hôm nay + 1 năm", c,
                     f"(toDate({q}) < toDate('2000-01-01') OR toDate({q}) > today() + 365)")

        names = set(names_of[t])
        if {"total_cost", "gallons", "price_per_gallon"} <= names:
            p = ("abs(toFloat64(total_cost) - toFloat64(gallons) * toFloat64(price_per_gallon)) "
                 "> greatest(0.02 * abs(toFloat64(total_cost)), 0.05)")
            pred(t, "Consistency", "total_cost = gallons x price_per_gallon (sai số 2%)", "total_cost", p)
        if {"total_revenue", "revenue", "fuel_surcharge", "accessorial_charges"} <= names:
            p = ("abs(toFloat64(total_revenue) - (toFloat64(revenue) + toFloat64(fuel_surcharge) "
                 "+ toFloat64(accessorial_charges))) > 0.05")
            pred(t, "Consistency", "total_revenue = revenue + fuel_surcharge + accessorial_charges", "total_revenue", p)

    for t in dims:
        cols = schema[t]
        sk = next((c for c, _ in cols if c.endswith("_key")), None)
        if sk:
            add(t, "Uniqueness", f"{sk} không trùng", sk, f"count() - uniqExact({qi(sk)})",
                sample_sql=f"SELECT * FROM {tbl(t)} WHERE {qi(sk)} IN (SELECT {qi(sk)} FROM {tbl(t)} GROUP BY {qi(sk)} HAVING count() > 1) LIMIT 50")
        nat = (sk[:-4] + "_id") if sk else None
        if nat and nat in names_of[t]:
            nq = qi(nat)
            add(t, "Uniqueness", f"{nat} không trùng", nat, f"count() - uniqExact({nq})",
                sample_sql=f"SELECT * FROM {tbl(t)} WHERE {nq} IN (SELECT {nq} FROM {tbl(t)} GROUP BY {nq} HAVING count() > 1) LIMIT 50")
            pred(t, "Missing key", f"{nat} rỗng", nat, f"{nq} = ''")
    return rules


RULES = build_rules()
rules_df = pd.DataFrame(RULES)

# ---------------------------------------------------------------------------
# Chạy kiểm tra
# ---------------------------------------------------------------------------
st.sidebar.divider()
st.sidebar.subheader("Tham số")
tolerance = st.sidebar.number_input(
    "Ngưỡng cho phép (% dòng lỗi)", 0.0, 100.0, 0.0, 0.1,
    help="Quy tắc đạt (PASS) nếu tỷ lệ dòng lỗi không vượt ngưỡng này. 0 nghĩa là không chấp nhận lỗi nào.",
)
st.sidebar.caption(f"{len(RULES)} quy tắc trên {len(facts)} bảng fact và {len(dims)} bảng dim.")


def run_checks() -> tuple:
    out, errors = [], []
    run_id = uuid.uuid4().hex[:12]
    ts = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    tables = list(dict.fromkeys(r["table"] for r in RULES))
    bar = st.progress(0.0, text="Đang kiểm tra...")
    for i, t in enumerate(tables):
        group = [r for r in RULES if r["table"] == t]
        select = ", ".join(["count() AS total"] + [f"{r['fail_expr']} AS f{j}" for j, r in enumerate(group)])
        try:
            res = run_query_slow(f"SELECT {select} FROM {tbl(t)}", creds).iloc[0]
            total = int(res["total"])
            for j, r in enumerate(group):
                failed = int(res[f"f{j}"] or 0)
                out.append({**{k: r[k] for k in ("table", "category", "rule", "column")},
                            "checked": total, "failed": failed, "sample_sql": r["sample_sql"]})
        except Exception as e:
            errors.append((t, str(e)[:300]))
        bar.progress((i + 1) / len(tables), text=f"Đã kiểm tra {t}")
    bar.empty()
    df = pd.DataFrame(out)
    return run_id, ts, df, errors


c1, c2 = st.columns([1, 3])
if c1.button("Chạy kiểm tra", type="primary"):
    st.session_state["dq"] = run_checks()
if c2.button("Xóa cache và chạy lại"):
    run_query_slow.clear()
    st.session_state["dq"] = run_checks()

if "dq" not in st.session_state:
    st.info(
        "Bấm **Chạy kiểm tra** để quét toàn bộ dòng của các bảng. Lần đầu có thể mất vài chục giây tùy dung lượng dữ liệu."
    )
    with st.expander(f"Danh sách {len(RULES)} quy tắc sẽ chạy"):
        st.dataframe(rules_df[["table", "category", "rule"]], width="stretch", hide_index=True)
    st.stop()

run_id, run_ts, df, errors = st.session_state["dq"]
if df.empty:
    st.error("Không có kết quả nào.")
    for t, e in errors:
        st.code(f"{t}: {e}")
    st.stop()

df = df.copy()
df["fail_pct"] = (df["failed"] / df["checked"].where(df["checked"] > 0) * 100).round(3)
df["status"] = (df["fail_pct"].fillna(0) <= tolerance).map({True: "PASS", False: "FAIL"})
df.loc[df["checked"] == 0, "status"] = "N/A"

# ---------------------------------------------------------------------------
# Giao diện
# ---------------------------------------------------------------------------
n_rules = len(df)
n_fail = int((df["status"] == "FAIL").sum())
rows_scanned = int(df.drop_duplicates("table")["checked"].sum())
k1, k2, k3, k4 = st.columns(4)
k1.metric("Quy tắc đã chạy", n_rules)
k2.metric("Đạt (PASS)", int((df["status"] == "PASS").sum()))
k3.metric("Không đạt (FAIL)", n_fail)
k4.metric("Dòng đã quét", f"{rows_scanned:,}")
st.caption(
    f"Lần chạy {run_id} lúc {run_ts:%d/%m/%Y %H:%M:%S} UTC. "
    "Mỗi quy tắc quét toàn bộ dòng của bảng tương ứng."
)
for t, e in errors:
    st.warning(f"Bảng {t} không kiểm tra được: {e}")

tab_sum, tab_rules, tab_sample, tab_hist = st.tabs(["Tổng quan", "Kết quả từng quy tắc", "Dòng lỗi mẫu", "Lịch sử"])

with tab_sum:
    by_cat = df.groupby("category").agg(
        rules=("rule", "count"),
        failed_rules=("status", lambda s: int((s == "FAIL").sum())),
        failed_rows=("failed", "sum"),
    ).reset_index()
    by_tbl = df.groupby("table").agg(
        rules=("rule", "count"),
        failed_rules=("status", lambda s: int((s == "FAIL").sum())),
        failed_rows=("failed", "sum"),
        rows=("checked", "max"),
    ).reset_index().sort_values("failed_rules", ascending=False)
    a, b = st.columns(2)
    a.write("Theo loại quy tắc")
    a.dataframe(by_cat.rename(columns={"category": "Loại", "rules": "Quy tắc", "failed_rules": "FAIL", "failed_rows": "Dòng lỗi"}),
                width="stretch", hide_index=True)
    b.write("Theo bảng")
    b.dataframe(by_tbl.rename(columns={"table": "Bảng", "rules": "Quy tắc", "failed_rules": "FAIL",
                                       "failed_rows": "Dòng lỗi", "rows": "Số dòng"}),
                width="stretch", hide_index=True)
    st.write("Số quy tắc FAIL theo bảng")
    st.bar_chart(by_tbl.set_index("table")["failed_rules"])

with tab_rules:
    f1, f2, f3 = st.columns(3)
    status_f = f1.multiselect("Trạng thái", ["FAIL", "PASS", "N/A"], default=["FAIL"])
    cat_f = f2.multiselect("Loại", sorted(df["category"].unique()))
    tbl_f = f3.multiselect("Bảng", sorted(df["table"].unique()))
    view = df
    if status_f:
        view = view[view["status"].isin(status_f)]
    if cat_f:
        view = view[view["category"].isin(cat_f)]
    if tbl_f:
        view = view[view["table"].isin(tbl_f)]
    show = view[["status", "table", "category", "rule", "checked", "failed", "fail_pct"]].sort_values(
        ["status", "failed"], ascending=[True, False]
    )
    st.dataframe(
        show.rename(columns={"status": "Kết quả", "table": "Bảng", "category": "Loại", "rule": "Quy tắc",
                             "checked": "Đã kiểm tra", "failed": "Dòng lỗi", "fail_pct": "% lỗi"}),
        width="stretch", hide_index=True,
        column_config={"Đã kiểm tra": st.column_config.NumberColumn(format="%,d"),
                       "Dòng lỗi": st.column_config.NumberColumn(format="%,d")},
    )
    log = df[["table", "category", "rule", "column", "checked", "failed", "fail_pct", "status"]].copy()
    log.insert(0, "run_ts", run_ts)
    log.insert(0, "run_id", run_id)
    st.download_button("Tải log kiểm tra (CSV)", log.to_csv(index=False).encode("utf-8"),
                       file_name=f"dq_log_{run_id}.csv", mime="text/csv")

    st.divider()
    st.write("**Ghi log vào warehouse**")
    st.caption(
        f"Tạo bảng {DB}.dq_log (nếu chưa có) và thêm kết quả lần chạy này, để theo dõi xu hướng theo thời gian. "
        "Cần tài khoản ClickHouse có quyền ghi. Tài khoản chỉ đọc sẽ báo lỗi, và việc đó không ảnh hưởng gì khác."
    )
    if st.button("Ghi log vào dq_log"):
        try:
            client = get_client(*creds)
            client.command(
                f"""CREATE TABLE IF NOT EXISTS {tbl('dq_log')} (
                    run_id String, run_ts DateTime, table_name String, category String, rule String,
                    column_name String, checked UInt64, failed UInt64, fail_pct Float64, status String
                ) ENGINE = MergeTree ORDER BY (run_ts, table_name)"""
            )
            out = log.rename(columns={"table": "table_name", "column": "column_name"}).copy()
            out["fail_pct"] = out["fail_pct"].fillna(0.0)
            client.insert_df(f"{DB}.dq_log", out[["run_id", "run_ts", "table_name", "category", "rule",
                                                   "column_name", "checked", "failed", "fail_pct", "status"]])
            st.success(f"Đã ghi {len(out)} dòng vào {DB}.dq_log.")
        except Exception as e:
            st.error("Không ghi được log.")
            st.code(str(e)[:500])

with tab_sample:
    failed = df[(df["status"] == "FAIL") & df["sample_sql"].notna()]
    if failed.empty:
        st.success("Không có quy tắc nào FAIL để xem mẫu.")
    else:
        labels = (failed["table"] + ": " + failed["rule"] + " (" + failed["failed"].astype(str) + " dòng)").tolist()
        pick_i = st.selectbox("Chọn quy tắc FAIL", range(len(labels)), format_func=lambda i: labels[i])
        sql = failed.iloc[pick_i]["sample_sql"]
        try:
            st.dataframe(run_query(sql, creds), width="stretch", hide_index=True)
        except Exception as e:
            st.error("Không lấy được mẫu.")
            st.code(str(e)[:500])
        with st.expander("Xem câu SQL"):
            st.code(sql, language="sql")

with tab_hist:
    try:
        hist = run_query(
            f"""SELECT run_id, min(run_ts) AS ts, countIf(status = 'PASS') AS passed, countIf(status = 'FAIL') AS failed_rules,
                       sum(failed) AS failed_rows
                FROM {tbl('dq_log')} GROUP BY run_id ORDER BY ts DESC LIMIT 50""",
            creds,
        )
        if hist.empty or "ts" not in hist.columns:
            st.info("Chưa có lần chạy nào được ghi vào dq_log.")
        else:
            st.dataframe(hist.rename(columns={"run_id": "Lần chạy", "ts": "Thời điểm (UTC)", "passed": "PASS",
                                              "failed_rules": "FAIL", "failed_rows": "Dòng lỗi"}),
                         width="stretch", hide_index=True)
            st.line_chart(hist.sort_values("ts").set_index("ts")[["passed", "failed_rules"]])
    except Exception:
        st.info("Chưa có bảng dq_log. Dùng nút 'Ghi log vào dq_log' ở tab Kết quả từng quy tắc để tạo.")
