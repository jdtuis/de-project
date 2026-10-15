"""BR-06: Seasonal Demand And Rate Analysis.

Xu hướng lịch sử theo tháng của: số lô (load volume), doanh thu, giá cước trung bình, số booking,
có thể chia theo tuyến, khách hàng hoặc loại booking. Có chỉ số mùa vụ để thấy tháng cao điểm và thấp điểm.
Nguồn: v_fact_trip (một dòng một chuyến, đã mang sẵn thông tin lô hàng) nối dim_date, dim_route, dim_customer.
"""

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from common import ensure_cols, get_creds, resolve_table, route_label_expr, run_query_slow

st.set_page_config(page_title="Seasonal demand", layout="wide")
creds = get_creds()

st.title("Seasonal demand and rates")
st.caption("Xu hướng theo tháng của số lô, doanh thu và giá cước, để nhận ra mùa cao điểm và lên kế hoạch năng lực.")

trip_t, _ = resolve_table(creds, "fact_trip")
date_t, _ = resolve_table(creds, "dim_date")
route_t, route_n = resolve_table(creds, "dim_route")
cust_t, _ = resolve_table(creds, "dim_customer")
ROUTE_EXPR = route_label_expr(creds, route_n)

# ---------------------------------------------------------------------------
# Tham số
# ---------------------------------------------------------------------------
st.sidebar.divider()
st.sidebar.subheader("Tham số")
dim = st.sidebar.selectbox("Chia theo", ["Không chia", "Tuyến", "Khách hàng", "Loại booking"])
metric_key = st.sidebar.selectbox(
    "Chỉ số",
    ["loads", "revenue", "rate_per_mile", "rev_per_load"],
    format_func={
        "loads": "Số lô (load volume)",
        "revenue": "Doanh thu",
        "rate_per_mile": "Giá cước trung bình ($/dặm)",
        "rev_per_load": "Doanh thu trung bình mỗi lô",
    }.get,
)
top_n = st.sidebar.slider("Số nhóm hiển thị (top theo số lô)", 3, 20, 6, disabled=(dim == "Không chia"))

GROUP_SQL = {
    "Không chia": ("'Tất cả'", ""),
    "Tuyến": ("ifNull(g.v, 'Chưa gán tuyến')",
              f"LEFT JOIN (SELECT route_key AS k, any({ROUTE_EXPR}) AS v FROM {route_t} GROUP BY k) AS g ON t.route_key = g.k"),
    "Khách hàng": ("ifNull(g.v, 'Chưa gán khách hàng')",
                   f"LEFT JOIN (SELECT customer_key AS k, any(customer_name) AS v FROM {cust_t} GROUP BY k) AS g ON t.customer_key = g.k"),
    "Loại booking": ("toString(t.booking_type)", ""),
}
grp_expr, grp_join = GROUP_SQL[dim]

# ---------------------------------------------------------------------------
# Truy vấn: một dòng một tháng một nhóm
# ---------------------------------------------------------------------------
SQL = f"""
SELECT toStartOfMonth(dd.full_date) AS mth,
       {grp_expr} AS grp,
       uniqExact(t.load_id) AS loads,
       count() AS trips,
       toFloat64(sum(t.total_revenue)) AS revenue,
       toFloat64(sum(t.actual_miles)) AS miles
FROM {trip_t} AS t
INNER JOIN {date_t} AS dd ON t.date_key = dd.date_key
{grp_join}
GROUP BY mth, grp
SETTINGS join_use_nulls = 1
"""
SQL_BOOKING = f"""
SELECT toStartOfMonth(dd.full_date) AS mth, toString(t.booking_type) AS booking_type, uniqExact(t.load_id) AS loads
FROM {trip_t} AS t
INNER JOIN {date_t} AS dd ON t.date_key = dd.date_key
GROUP BY mth, booking_type
"""

try:
    raw = run_query_slow(SQL, creds)
    booking = run_query_slow(SQL_BOOKING, creds)
except Exception as e:
    st.error("Truy vấn bị lỗi.")
    st.code(str(e))
    st.stop()

raw = ensure_cols(raw, ["mth", "grp", "loads", "trips", "revenue", "miles"])
booking = ensure_cols(booking, ["mth", "booking_type", "loads"])
if raw.empty:
    st.warning("Chưa có dữ liệu chuyến đi.")
    st.stop()
raw["mth"] = pd.to_datetime(raw["mth"])
booking["mth"] = pd.to_datetime(booking["mth"])

months_all = sorted(raw["mth"].unique())
lo, hi = pd.Timestamp(months_all[0]).date(), pd.Timestamp(months_all[-1]).date()
rng = st.sidebar.date_input("Khoảng ngày", value=(lo, hi), min_value=lo, max_value=hi)
if isinstance(rng, (tuple, list)) and len(rng) == 2:
    raw = raw[(raw["mth"] >= pd.Timestamp(rng[0]).to_period("M").to_timestamp()) & (raw["mth"] <= pd.Timestamp(rng[1]))]
    booking = booking[(booking["mth"] >= pd.Timestamp(rng[0]).to_period("M").to_timestamp()) & (booking["mth"] <= pd.Timestamp(rng[1]))]
if raw.empty:
    st.warning("Không có dữ liệu trong khoảng ngày đã chọn.")
    st.stop()


def add_metrics(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rate_per_mile"] = df["revenue"] / df["miles"].where(df["miles"] > 0)
    df["rev_per_load"] = df["revenue"] / df["loads"].where(df["loads"] > 0)
    return df


raw = add_metrics(raw)
LABEL = {"loads": "Số lô", "revenue": "Doanh thu", "rate_per_mile": "Giá cước ($/dặm)", "rev_per_load": "Doanh thu/lô"}

# Chọn nhóm hiển thị: top theo tổng số lô
order = raw.groupby("grp")["loads"].sum().sort_values(ascending=False)
groups = order.index.tolist()[:top_n] if dim != "Không chia" else order.index.tolist()
if dim != "Không chia":
    groups = st.multiselect(f"{dim} hiển thị", order.index.tolist(), default=groups)
    if not groups:
        st.info("Chọn ít nhất một nhóm.")
        st.stop()
data = raw[raw["grp"].isin(groups)]

# ---------------------------------------------------------------------------
# Chỉ số tổng quan
# ---------------------------------------------------------------------------
tot = data[["loads", "revenue", "miles"]].sum()
n_months = data["mth"].nunique()
k1, k2, k3, k4 = st.columns(4)
k1.metric("Tổng số lô", f"{int(tot['loads']):,}")
k2.metric("Doanh thu", f"${tot['revenue']:,.0f}")
k3.metric("Giá cước trung bình", f"${tot['revenue'] / tot['miles']:.2f}/dặm" if tot["miles"] else "n/a")
k4.metric("Số tháng có dữ liệu", n_months)

tab_trend, tab_season, tab_book = st.tabs(["Xu hướng theo tháng", "Tính mùa vụ", "Cơ cấu booking"])

# ---------------------------------------------------------------------------
# Xu hướng theo tháng
# ---------------------------------------------------------------------------
with tab_trend:
    pivot = data.pivot_table(index="mth", columns="grp", values=metric_key, aggfunc="sum" if metric_key in ("loads", "revenue") else "mean")
    if metric_key in ("rate_per_mile", "rev_per_load"):
        # tính lại đúng theo tổng, không lấy trung bình các tỷ lệ
        agg = data.groupby(["mth", "grp"])[["loads", "revenue", "miles"]].sum().reset_index()
        agg = add_metrics(agg)
        pivot = agg.pivot_table(index="mth", columns="grp", values=metric_key, aggfunc="first")
    st.write(f"{LABEL[metric_key]} theo tháng")
    st.line_chart(pivot)
    table = pivot.copy()
    table.index = table.index.strftime("%Y-%m")
    st.dataframe(table.sort_index(ascending=False).round(2), width="stretch")
    st.download_button("Tải CSV", table.to_csv().encode("utf-8"), file_name="monthly_trend.csv", mime="text/csv")

# ---------------------------------------------------------------------------
# Tính mùa vụ
# ---------------------------------------------------------------------------
with tab_season:
    st.caption(
        "Chỉ số mùa vụ của một tháng trong năm = giá trị trung bình của tháng đó qua các năm chia cho giá trị trung bình "
        "của cả kỳ. 1,20 nghĩa là cao hơn trung bình 20%. Cần ít nhất 12 tháng dữ liệu."
    )
    if n_months < 12:
        st.info(f"Mới có {n_months} tháng dữ liệu, chưa đủ để đánh giá tính mùa vụ.")
    else:
        agg = data.groupby(["mth", "grp"])[["loads", "revenue", "miles"]].sum().reset_index()
        agg = add_metrics(agg)
        agg["moy"] = agg["mth"].dt.month
        rows, idx_rows = [], []
        for g, sub in agg.groupby("grp"):
            overall = sub[metric_key].mean()
            if not overall or np.isnan(overall):
                continue
            by_month = sub.groupby("moy")[metric_key].mean() / overall
            for m, v in by_month.items():
                idx_rows.append({"Tháng": m, "Nhóm": g, "Chỉ số": v})
            if len(by_month) >= 6:
                rows.append({
                    "Nhóm": g,
                    "Tháng cao điểm": int(by_month.idxmax()),
                    "Chỉ số cao điểm": round(by_month.max(), 2),
                    "Tháng thấp điểm": int(by_month.idxmin()),
                    "Chỉ số thấp điểm": round(by_month.min(), 2),
                    "Biên độ (cao/thấp)": round(by_month.max() / by_month.min(), 2) if by_month.min() > 0 else np.nan,
                })
        idx = pd.DataFrame(idx_rows)
        if idx.empty:
            st.info("Không đủ dữ liệu để tính chỉ số mùa vụ.")
        else:
            st.write(f"Chỉ số mùa vụ của {LABEL[metric_key].lower()} theo tháng trong năm")
            chart = alt.Chart(idx).mark_line(point=True).encode(
                x=alt.X("Tháng:O", title="Tháng trong năm"),
                y=alt.Y("Chỉ số:Q", scale=alt.Scale(zero=False)),
                color="Nhóm:N",
                tooltip=["Nhóm", "Tháng", alt.Tooltip("Chỉ số:Q", format=".2f")],
            )
            st.altair_chart(chart, width="stretch")
            if rows:
                st.write("So sánh giữa các nhóm")
                st.dataframe(pd.DataFrame(rows).sort_values("Biên độ (cao/thấp)", ascending=False),
                             width="stretch", hide_index=True)

        total = add_metrics(data.groupby("mth")[["loads", "revenue", "miles"]].sum().reset_index())
        total["Năm"] = total["mth"].dt.year.astype(str)
        total["Tháng"] = total["mth"].dt.month
        heat = alt.Chart(total).mark_rect().encode(
            x=alt.X("Tháng:O", title="Tháng"),
            y=alt.Y("Năm:O"),
            color=alt.Color(f"{metric_key}:Q", title=LABEL[metric_key], scale=alt.Scale(scheme="blues")),
            tooltip=["Năm", "Tháng", alt.Tooltip(f"{metric_key}:Q", format=",.2f")],
        )
        st.write(f"{LABEL[metric_key]} theo năm và tháng (các nhóm đã chọn gộp lại)")
        st.altair_chart(heat, width="stretch")

# ---------------------------------------------------------------------------
# Cơ cấu booking
# ---------------------------------------------------------------------------
with tab_book:
    st.caption("Số lô theo loại booking mỗi tháng, tính trên toàn bộ dữ liệu trong khoảng ngày, không phụ thuộc phần Chia theo.")
    if booking.empty:
        st.info("Không có dữ liệu booking.")
    else:
        bp = booking.pivot_table(index="mth", columns="booking_type", values="loads", aggfunc="sum").fillna(0)
        st.bar_chart(bp)
        share = bp.div(bp.sum(axis=1), axis=0) * 100
        share.index = share.index.strftime("%Y-%m")
        st.write("Tỷ trọng mỗi loại booking (%)")
        st.dataframe(share.sort_index(ascending=False).round(1), width="stretch")
