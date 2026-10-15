"""BR-01: Fleet Performance & Predictive Maintenance.

Mỗi xe, mỗi tháng:
  - on-time rate : v_fact_delivery_event (is_on_time), nối v_fact_trip để biết xe nào giao
  - MPG          : tổng actual_miles (v_fact_trip) / tổng gallons (v_fact_fuel_purchase)
  - quãng đường  : tổng actual_miles (v_fact_trip)
So với median của đội xe trong cùng tháng, rồi gắn cờ nhóm thấp nhất (mặc định 10%).
"""

import numpy as np
import pandas as pd
import streamlit as st

from common import get_creds, resolve_table, run_query_slow

st.set_page_config(page_title="Fleet performance", layout="wide")
creds = get_creds()

st.title("Fleet performance")
st.caption("Hiệu suất từng xe theo tháng, so với median của đội xe, để chọn xe cần kiểm tra bảo trì hoặc thay thế.")

trip_t, _ = resolve_table(creds, "fact_trip")
fuel_t, _ = resolve_table(creds, "fact_fuel_purchase")
del_t, _ = resolve_table(creds, "fact_delivery_event")
truck_t, _ = resolve_table(creds, "dim_truck")
date_t, _ = resolve_table(creds, "dim_date")

# ---------------------------------------------------------------------------
# Tham số
# ---------------------------------------------------------------------------
st.sidebar.divider()
st.sidebar.subheader("Tham số")
pct = st.sidebar.slider("Gắn cờ nhóm thấp nhất (%)", 5, 30, 10)
min_deliveries = st.sidebar.number_input("Số lượt giao tối thiểu để tính on-time", 1, 50, 3)
gal_source = st.sidebar.radio(
    "Nguồn gallons để tính MPG",
    ["Mua nhiên liệu (fact_fuel_purchase)", "Ước tính theo chuyến (fuel_consumed_gallons)"],
    help="BR-01 dùng fuel_purchases. Ngày mua nhiên liệu không trùng ngày chạy nên MPG từng tháng có thể dao động. "
         "Nguồn theo chuyến ổn định hơn nếu bảng trips có đủ dữ liệu.",
)

# ---------------------------------------------------------------------------
# Truy vấn tổng hợp trong ClickHouse (mỗi truy vấn trả về xe x tháng)
# ---------------------------------------------------------------------------
sql_trip = f"""
SELECT t.truck_key AS tk,
       toStartOfMonth(dd.full_date) AS mth,
       toFloat64(sum(t.actual_miles)) AS miles,
       toFloat64(sum(t.fuel_consumed_gallons)) AS trip_gal,
       count() AS n_trips
FROM {trip_t} AS t
INNER JOIN {date_t} AS dd ON t.date_key = dd.date_key
GROUP BY tk, mth
"""

sql_fuel = f"""
SELECT f.truck_key AS tk,
       toStartOfMonth(dd.full_date) AS mth,
       toFloat64(sum(f.gallons)) AS fuel_gal
FROM {fuel_t} AS f
INNER JOIN {date_t} AS dd ON f.date_key = dd.date_key
GROUP BY tk, mth
"""

sql_deliv = f"""
SELECT t.tkey AS tk,
       toStartOfMonth(toDate(d.scheduled_datetime)) AS mth,
       countIf(isNotNull(d.actual_datetime)) AS delivered,
       countIf(isNotNull(d.actual_datetime) AND d.is_on_time = 1) AS on_time
FROM {del_t} AS d
INNER JOIN (
    SELECT trip_key AS trk, any(truck_key) AS tkey
    FROM {trip_t}
    GROUP BY trk
) AS t ON d.trip_key = t.trk
WHERE positionCaseInsensitive(d.event_type, 'deliver') > 0
GROUP BY tk, mth
"""

sql_labels = f"""
SELECT truck_key AS tk, any(unit_number) AS unit
FROM {truck_t}
GROUP BY tk
"""

try:
    trip = run_query_slow(sql_trip, creds)
    fuel = run_query_slow(sql_fuel, creds)
    deliv = run_query_slow(sql_deliv, creds)
    labels = run_query_slow(sql_labels, creds)
except Exception as e:
    st.error("Truy vấn bị lỗi.")
    st.code(str(e))
    st.stop()

# ---------------------------------------------------------------------------
# Ghép dữ liệu, tính median, điểm và cờ
# ---------------------------------------------------------------------------
df = trip.merge(fuel, on=["tk", "mth"], how="outer").merge(deliv, on=["tk", "mth"], how="outer")
df["month"] = pd.to_datetime(df["mth"])
df = df[df["miles"].fillna(0) > 0].copy()  # chỉ tính các xe có chạy trong tháng

if df.empty:
    st.warning("Không có dữ liệu chuyến đi để tính toán.")
    st.stop()

gal = df["fuel_gal"] if gal_source.startswith("Mua") else df["trip_gal"]
df["mpg"] = np.where(gal.fillna(0) > 0, df["miles"] / gal, np.nan)
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
df = df.merge(labels, on="tk", how="left")
df["truck"] = df["unit"].fillna(df["tk"].astype(str))

# ---------------------------------------------------------------------------
# Giao diện
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
    flagged_view = flagged[list(show_cols)].rename(columns=show_cols)
    st.dataframe(flagged_view, width="stretch", hide_index=True, column_config=cfg)
    st.download_button(
        "Tải CSV", flagged_view.to_csv(index=False).encode("utf-8"),
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
    for c, m in zip(st.columns(3), METRICS):
        c.write(NAMES[m])
        chart = pd.DataFrame(
            {"Xe này": one[m], "Median đội xe": med.set_index("month")[f"{m}_median"]}
        ).dropna(how="all")
        c.line_chart(chart)
