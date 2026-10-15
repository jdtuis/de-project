"""BR-01: Fleet Performance & Predictive Maintenance.

Mỗi xe, mỗi tháng:
  - on-time rate : delivery_events (nối trips để biết xe nào giao)
  - MPG          : tổng actual_distance_miles (trips) / tổng gallons (fuel_purchases)
  - quãng đường  : tổng actual_distance_miles (trips)
So với median của đội xe trong cùng tháng, rồi gắn cờ nhóm thấp nhất (mặc định 10%).
"""

import math

import numpy as np
import pandas as pd
import streamlit as st

from common import dt, get_creds, num, pick, qi, require, resolve_table, run_query_slow, table_columns

st.set_page_config(page_title="Fleet performance", layout="wide")
creds = get_creds()

st.title("Fleet performance")
st.caption("Hiệu suất từng xe theo tháng, so với median của đội xe, để chọn xe cần kiểm tra bảo trì hoặc thay thế.")

# ---------------------------------------------------------------------------
# 1. Tìm bảng và cột
# ---------------------------------------------------------------------------
trips_t, trips_n = resolve_table(creds, "trips")
fuel_t, fuel_n = resolve_table(creds, "fuel_purchases")
del_t, del_n = resolve_table(creds, "delivery_events")
trucks_t, trucks_n = resolve_table(creds, "trucks", required=False)

tc = table_columns(creds, trips_n)
fc = table_columns(creds, fuel_n)
dc = table_columns(creds, del_n)

col = {
    "trips.truck_id": pick(tc, "truck_id"),
    "trips.trip_id": pick(tc, "trip_id"),
    "trips.ngày chạy": pick(tc, "dispatch_date", "trip_date", "start_date", "start_time", "departure_time"),
    "trips.actual_distance_miles": pick(tc, "actual_distance_miles", "distance_miles"),
    "fuel_purchases.truck_id": pick(fc, "truck_id"),
    "fuel_purchases.ngày mua": pick(fc, "purchase_date", "purchase_datetime", "fuel_date"),
    "fuel_purchases.gallons": pick(fc, "gallons", "gallons_purchased"),
    "delivery_events.trip_id": pick(dc, "trip_id"),
    "delivery_events.scheduled": pick(dc, "scheduled_datetime", "scheduled_time", "scheduled_date", "scheduled_at"),
    "delivery_events.actual": pick(dc, "actual_datetime", "actual_time", "actual_date", "actual_at"),
}
require(col, {trips_n: tc, fuel_n: fc, del_n: dc})
event_col = pick(dc, "event_type")

with st.expander("Bảng và cột đang dùng"):
    st.write({"trips": trips_n, "fuel_purchases": fuel_n, "delivery_events": del_n, **col})

# ---------------------------------------------------------------------------
# 2. Truy vấn tổng hợp trong ClickHouse (mỗi truy vấn trả về xe x tháng)
# ---------------------------------------------------------------------------
TRIP_DATE = dt(col["trips.ngày chạy"])
FUEL_DATE = dt(col["fuel_purchases.ngày mua"])
SCHED = dt(col["delivery_events.scheduled"])
ACTUAL = dt(col["delivery_events.actual"])
EVENT_FILTER = (
    f"positionCaseInsensitive(toString({qi(event_col)}), 'deliver') > 0" if event_col else "1"
)

sql_dist = f"""
SELECT toString({qi(col['trips.truck_id'])}) AS tk,
       toStartOfMonth(toDate({TRIP_DATE})) AS mth,
       sum({num(col['trips.actual_distance_miles'])}) AS miles,
       count() AS n_trips
FROM {trips_t}
WHERE {TRIP_DATE} IS NOT NULL
GROUP BY tk, mth
"""

sql_fuel = f"""
SELECT toString({qi(col['fuel_purchases.truck_id'])}) AS tk,
       toStartOfMonth(toDate({FUEL_DATE})) AS mth,
       sum({num(col['fuel_purchases.gallons'])}) AS gal
FROM {fuel_t}
WHERE {FUEL_DATE} IS NOT NULL
GROUP BY tk, mth
"""

sql_deliv = f"""
SELECT t.tk AS tk,
       toStartOfMonth(toDate(d.sched)) AS mth,
       countIf(isNotNull(d.act)) AS delivered,
       countIf(ifNull(d.act <= d.sched, 0)) AS on_time
FROM (
    SELECT toString({qi(col['delivery_events.trip_id'])}) AS tr,
           {SCHED} AS sched,
           {ACTUAL} AS act
    FROM {del_t}
    WHERE {EVENT_FILTER} AND {SCHED} IS NOT NULL
) AS d
INNER JOIN (
    SELECT toString({qi(col['trips.trip_id'])}) AS tr,
           any(toString({qi(col['trips.truck_id'])})) AS tk
    FROM {trips_t}
    GROUP BY tr
) AS t ON d.tr = t.tr
GROUP BY tk, mth
"""

try:
    dist = run_query_slow(sql_dist, creds)
    fuel = run_query_slow(sql_fuel, creds)
    deliv = run_query_slow(sql_deliv, creds)
except Exception as e:
    st.error("Truy vấn bị lỗi.")
    st.code(str(e))
    st.stop()

labels = None
if trucks_t:
    kc = table_columns(creds, trucks_n)
    tk_col, unit_col = pick(kc, "truck_id"), pick(kc, "unit_number", "unit_id", "truck_name", "name")
    if tk_col and unit_col:
        labels = run_query_slow(
            f"SELECT toString({qi(tk_col)}) AS tk, any(toString({qi(unit_col)})) AS unit "
            f"FROM {trucks_t} GROUP BY tk",
            creds,
        )

# ---------------------------------------------------------------------------
# 3. Tham số
# ---------------------------------------------------------------------------
st.sidebar.divider()
st.sidebar.subheader("Tham số")
pct = st.sidebar.slider("Gắn cờ nhóm thấp nhất (%)", 5, 30, 10)
min_deliveries = st.sidebar.number_input("Số lượt giao tối thiểu để tính on-time", 1, 50, 3)

# ---------------------------------------------------------------------------
# 4. Ghép dữ liệu, tính median, điểm và cờ
# ---------------------------------------------------------------------------
df = dist.merge(fuel, on=["tk", "mth"], how="outer").merge(deliv, on=["tk", "mth"], how="outer")
df["month"] = pd.to_datetime(df["mth"])
df = df[df["miles"].fillna(0) > 0].copy()  # chỉ tính các xe có chạy trong tháng

if df.empty:
    st.warning("Không có dữ liệu chuyến đi để tính toán.")
    st.stop()

df["mpg"] = np.where(df["gal"].fillna(0) > 0, df["miles"] / df["gal"], np.nan)
df["on_time_rate"] = np.where(
    df["delivered"].fillna(0) >= min_deliveries, df["on_time"] / df["delivered"] * 100, np.nan
)

METRICS = ["on_time_rate", "mpg", "miles"]
NAMES = {"on_time_rate": "On-time (%)", "mpg": "MPG", "miles": "Quãng đường (dặm)"}

med = df.groupby("month")[METRICS].median().add_suffix("_median").reset_index()
df = df.merge(med, on="month", how="left")

# Điểm tổng hợp = trung bình thứ hạng phần trăm của 3 chỉ số trong cùng tháng (cao là tốt)
for m in METRICS:
    df[f"{m}_pct"] = df.groupby("month")[m].rank(pct=True)
df["n_metrics"] = df[[f"{m}_pct" for m in METRICS]].notna().sum(axis=1)
df["score"] = df[[f"{m}_pct" for m in METRICS]].mean(axis=1)
df.loc[df["n_metrics"] < 2, "score"] = np.nan  # cần ít nhất 2 chỉ số mới xếp hạng

df["score_rank"] = df.groupby("month")["score"].rank(method="first")
df["n_valid"] = df.groupby("month")["score"].transform("count")
df["k"] = np.maximum(1, np.ceil(df["n_valid"] * pct / 100))
df["flagged"] = df["score"].notna() & (df["score_rank"] <= df["k"])


def why(r):
    out = []
    if pd.notna(r["on_time_rate"]) and r["on_time_rate"] < r["on_time_rate_median"]:
        out.append("on-time thấp")
    if pd.notna(r["mpg"]) and r["mpg"] < r["mpg_median"]:
        out.append("MPG thấp")
    if pd.notna(r["miles"]) and r["miles"] < r["miles_median"]:
        out.append("ít quãng đường")
    return ", ".join(out)


df["reasons"] = df.apply(why, axis=1)
if labels is not None:
    df = df.merge(labels, on="tk", how="left")
    df["truck"] = df["unit"].fillna(df["tk"])
else:
    df["truck"] = df["tk"]

# ---------------------------------------------------------------------------
# 5. Giao diện
# ---------------------------------------------------------------------------
months = sorted(df["month"].unique(), reverse=True)
sel = st.selectbox("Tháng", months, format_func=lambda m: pd.Timestamp(m).strftime("%m/%Y"))
sub = df[df["month"] == sel]
m_row = med[med["month"] == sel].iloc[0]

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Số xe hoạt động", len(sub))
k2.metric("On-time median", f"{m_row['on_time_rate_median']:.1f}%" if pd.notna(m_row["on_time_rate_median"]) else "n/a")
k3.metric("MPG median", f"{m_row['mpg_median']:.2f}" if pd.notna(m_row["mpg_median"]) else "n/a")
k4.metric("Dặm median", f"{m_row['miles_median']:,.0f}")
k5.metric("Xe bị gắn cờ", int(sub["flagged"].sum()))

tab_flag, tab_all, tab_trend, tab_truck = st.tabs(
    ["Xe cần chú ý", "Toàn bộ đội xe", "Xu hướng đội xe", "Chi tiết từng xe"]
)

show_cols = {
    "truck": "Xe",
    "on_time_rate": "On-time (%)",
    "on_time_rate_median": "On-time median",
    "mpg": "MPG",
    "mpg_median": "MPG median",
    "miles": "Dặm",
    "miles_median": "Dặm median",
    "score": "Điểm",
    "reasons": "Lý do",
}
cfg = {
    "On-time (%)": st.column_config.NumberColumn(format="%.1f"),
    "On-time median": st.column_config.NumberColumn(format="%.1f"),
    "MPG": st.column_config.NumberColumn(format="%.2f"),
    "MPG median": st.column_config.NumberColumn(format="%.2f"),
    "Dặm": st.column_config.NumberColumn(format="%,.0f"),
    "Dặm median": st.column_config.NumberColumn(format="%,.0f"),
    "Điểm": st.column_config.NumberColumn(format="%.2f"),
}

with tab_flag:
    st.caption(
        f"Điểm là trung bình thứ hạng phần trăm của on-time, MPG và quãng đường trong cùng tháng. "
        f"Các xe có điểm thấp nhất ({pct}%) bị gắn cờ."
    )
    flagged = sub[sub["flagged"]].sort_values("score")
    st.dataframe(
        flagged[list(show_cols)].rename(columns=show_cols),
        width="stretch", hide_index=True, column_config=cfg,
    )
    st.download_button(
        "Tải CSV", flagged[list(show_cols)].rename(columns=show_cols).to_csv(index=False).encode("utf-8"),
        file_name=f"flagged_trucks_{pd.Timestamp(sel):%Y_%m}.csv", mime="text/csv",
    )

    st.subheader("Xe bị gắn cờ lặp lại trong 6 tháng gần nhất")
    recent = [m for m in months if m <= sel][:6]
    rep = (
        df[df["month"].isin(recent)]
        .groupby("truck")["flagged"].sum().astype(int)
        .sort_values(ascending=False)
    )
    rep = rep[rep >= 2].rename("Số tháng bị gắn cờ").reset_index().rename(columns={"truck": "Xe"})
    if rep.empty:
        st.info("Chưa có xe nào bị gắn cờ từ 2 tháng trở lên.")
    else:
        st.dataframe(rep, width="stretch", hide_index=True)

with tab_all:
    st.dataframe(
        sub.sort_values("score")[list(show_cols)].rename(columns=show_cols),
        width="stretch", hide_index=True, column_config=cfg,
    )

with tab_trend:
    t = med.set_index("month")
    c1, c2, c3 = st.columns(3)
    c1.write("On-time median (%)")
    c1.line_chart(t["on_time_rate_median"])
    c2.write("MPG median")
    c2.line_chart(t["mpg_median"])
    c3.write("Dặm median")
    c3.line_chart(t["miles_median"])

with tab_truck:
    default = flagged["truck"].iloc[0] if len(flagged) else sub["truck"].iloc[0]
    trucks = sorted(df["truck"].unique())
    choice = st.selectbox("Chọn xe", trucks, index=trucks.index(default))
    one = df[df["truck"] == choice].set_index("month").sort_index()
    cols = st.columns(3)
    for c, m in zip(cols, METRICS):
        c.write(NAMES[m])
        chart = pd.DataFrame({"Xe này": one[m], "Median đội xe": med.set_index("month")[f"{m}_median"]}).dropna(how="all")
        c.line_chart(chart)
