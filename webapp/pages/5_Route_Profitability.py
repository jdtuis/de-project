"""BR-05: Analyze Route Profitability.

Theo tuyến (origin-destination) và kỳ báo cáo:
  - Doanh thu        : total_revenue của chuyến (v_fact_trip)
  - Chi phí nhiên liệu: total_cost của v_fact_fuel_purchase, gán về tuyến qua trip_key
  - Chi phí bảo trì  : maintenance_cost của fact_truck_monthly, phân bổ cho chuyến theo số dặm
                       (chi phí bảo trì / tổng dặm của xe trong tháng x số dặm của chuyến)
  - Doanh thu/dặm, chi phí/dặm, lợi nhuận, biên lợi nhuận
Chi phí tài xế, phí cầu đường, khấu hao không có trong dữ liệu nên không được tính.
"""

import pandas as pd
import streamlit as st

from common import (
    ensure_cols, get_creds, pick, qi, resolve_table, route_label_expr, run_query_slow, table_columns,
)

st.set_page_config(page_title="Route profitability", layout="wide")
creds = get_creds()

st.title("Route profitability")
st.caption("Doanh thu và chi phí vận chuyển theo tuyến và kỳ báo cáo, để so sánh hiệu quả tài chính giữa các tuyến.")

trip_t, _ = resolve_table(creds, "fact_trip")
fuel_t, _ = resolve_table(creds, "fact_fuel_purchase")
date_t, _ = resolve_table(creds, "dim_date")
route_t, route_n = resolve_table(creds, "dim_route")
tm_t, tm_n = resolve_table(creds, "fact_truck_monthly", required=False)

ROUTE_EXPR = route_label_expr(creds, route_n)

# Bảo trì phân bổ: cần cột chi phí bảo trì và tổng dặm trong fact_truck_monthly
maint_cost_col = maint_miles_col = None
if tm_t:
    tmc = table_columns(creds, tm_n)
    maint_cost_col = pick(tmc, "maintenance_cost", "total_maintenance_cost", "maintenance_cost_usd")
    maint_miles_col = pick(tmc, "total_miles", "miles", "actual_miles")
can_allocate = bool(maint_cost_col and maint_miles_col and "truck_key" in tmc and "month_date_key" in tmc)

# ---------------------------------------------------------------------------
# Tham số
# ---------------------------------------------------------------------------
try:
    bounds = run_query_slow(
        f"SELECT min(dd.full_date) AS lo, max(dd.full_date) AS hi FROM {trip_t} AS t "
        f"INNER JOIN {date_t} AS dd ON t.date_key = dd.date_key",
        creds,
    )
except Exception as e:
    st.error("Không đọc được dữ liệu chuyến đi.")
    st.code(str(e))
    st.stop()
bounds = ensure_cols(bounds, ["lo", "hi"])
if bounds["lo"].isna().all():
    st.warning("Chưa có dữ liệu chuyến đi.")
    st.stop()
lo, hi = pd.to_datetime(bounds["lo"].iloc[0]).date(), pd.to_datetime(bounds["hi"].iloc[0]).date()

st.sidebar.divider()
st.sidebar.subheader("Tham số")
gran = st.sidebar.radio("Kỳ báo cáo", ["Tháng", "Quý", "Năm"])
rng = st.sidebar.date_input("Khoảng ngày", value=(lo, hi), min_value=lo, max_value=hi)
use_maint = st.sidebar.toggle(
    "Tính chi phí bảo trì (phân bổ theo dặm)", value=can_allocate, disabled=not can_allocate,
    help="Bảo trì được ghi theo xe và tháng, không theo tuyến, nên phải phân bổ theo số dặm của chuyến.",
)
min_ship = st.sidebar.number_input("Số lô tối thiểu mỗi tuyến", 1, 500, 5)
if not can_allocate:
    st.sidebar.caption("Không tìm thấy cột chi phí bảo trì và tổng dặm trong fact_truck_monthly, nên bỏ qua bảo trì.")

if not (isinstance(rng, (tuple, list)) and len(rng) == 2):
    st.info("Chọn đủ ngày bắt đầu và ngày kết thúc.")
    st.stop()
d0, d1 = rng[0].isoformat(), rng[1].isoformat()
PERIOD = {"Tháng": "toStartOfMonth", "Quý": "toStartOfQuarter", "Năm": "toStartOfYear"}[gran]

# ---------------------------------------------------------------------------
# Truy vấn: một dòng một tuyến một kỳ
# ---------------------------------------------------------------------------
if use_maint:
    maint_join = f"""
    LEFT JOIN (
        SELECT m.truck_key AS k, toStartOfMonth(dm.full_date) AS mth,
               toFloat64(sum(m.{qi(maint_cost_col)})) / nullIf(toFloat64(sum(m.{qi(maint_miles_col)})), 0) AS rate
        FROM {tm_t} AS m
        INNER JOIN {date_t} AS dm ON m.month_date_key = dm.date_key
        GROUP BY k, mth
    ) AS mc ON b.truck_key = mc.k AND b.mth = mc.mth"""
    maint_expr = "ifNull(mc.rate, 0) * ifNull(b.actual_miles, 0)"
    maint_has = "isNotNull(mc.rate)"
else:
    maint_join, maint_expr, maint_has = "", "0", "0"

SQL = f"""
SELECT per, route,
       count() AS shipments,
       toFloat64(sum(miles)) AS miles,
       sum(rev) AS revenue,
       sum(fuel) AS fuel_cost,
       sum(maint) AS maint_cost,
       countIf(has_fuel) AS with_fuel,
       countIf(has_maint) AS with_maint
FROM (
    SELECT {PERIOD}(b.ship_date) AS per,
           ifNull(rt.rl, 'Chưa gán tuyến') AS route,
           b.actual_miles AS miles,
           ifNull(b.total_revenue, 0) AS rev,
           ifNull(fu.fc, 0) AS fuel,
           {maint_expr} AS maint,
           isNotNull(fu.fc) AS has_fuel,
           {maint_has} AS has_maint
    FROM (
        SELECT t.trip_key AS trip_key, t.route_key AS route_key, t.truck_key AS truck_key,
               t.actual_miles AS actual_miles, toFloat64(t.total_revenue) AS total_revenue,
               dd.full_date AS ship_date, toStartOfMonth(dd.full_date) AS mth
        FROM {trip_t} AS t
        INNER JOIN {date_t} AS dd ON t.date_key = dd.date_key
        WHERE dd.full_date BETWEEN toDate('{d0}') AND toDate('{d1}')
    ) AS b
    LEFT JOIN (SELECT route_key AS k, any({ROUTE_EXPR}) AS rl FROM {route_t} GROUP BY k) AS rt ON b.route_key = rt.k
    LEFT JOIN (SELECT trip_key AS k, toFloat64(sum(total_cost)) AS fc FROM {fuel_t} GROUP BY k) AS fu ON b.trip_key = fu.k
    {maint_join}
)
GROUP BY per, route
SETTINGS join_use_nulls = 1
"""

try:
    raw = run_query_slow(SQL, creds)
except Exception as e:
    st.error("Truy vấn lợi nhuận theo tuyến bị lỗi.")
    st.code(str(e))
    st.stop()

raw = ensure_cols(raw, ["per", "route", "shipments", "miles", "revenue", "fuel_cost", "maint_cost", "with_fuel", "with_maint"])
if raw.empty:
    st.warning("Không có chuyến nào trong khoảng ngày đã chọn.")
    st.stop()
raw["per"] = pd.to_datetime(raw["per"])


def derive(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["total_cost"] = df["fuel_cost"] + df["maint_cost"]
    df["profit"] = df["revenue"] - df["total_cost"]
    df["margin_pct"] = (df["profit"] / df["revenue"].where(df["revenue"] > 0) * 100).round(1)
    miles = df["miles"].where(df["miles"] > 0)
    df["rev_per_mile"] = (df["revenue"] / miles).round(2)
    df["cost_per_mile"] = (df["total_cost"] / miles).round(2)
    df["profit_per_mile"] = (df["profit"] / miles).round(2)
    return df


by_route = derive(
    raw.groupby("route", as_index=False)[
        ["shipments", "miles", "revenue", "fuel_cost", "maint_cost", "with_fuel", "with_maint"]
    ].sum()
)
by_route = by_route[by_route["shipments"] >= min_ship]
if by_route.empty:
    st.warning("Không có tuyến nào đạt số lô tối thiểu. Giảm ngưỡng ở thanh bên.")
    st.stop()

# ---------------------------------------------------------------------------
# Chỉ số tổng quan
# ---------------------------------------------------------------------------
tot = derive(raw[["shipments", "miles", "revenue", "fuel_cost", "maint_cost"]].sum().to_frame().T).iloc[0]
k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("Số lô", f"{int(tot['shipments']):,}")
k2.metric("Doanh thu", f"${tot['revenue']:,.0f}")
k3.metric("Chi phí", f"${tot['total_cost']:,.0f}")
k4.metric("Lợi nhuận", f"${tot['profit']:,.0f}")
k5.metric("Biên lợi nhuận", f"{tot['margin_pct']:.1f}%" if pd.notna(tot["margin_pct"]) else "n/a")
k6.metric("Doanh thu / dặm", f"${tot['rev_per_mile']:.2f}" if pd.notna(tot["rev_per_mile"]) else "n/a")

tab_route, tab_period, tab_cov = st.tabs(["Theo tuyến", f"Theo {gran.lower()}", "Độ phủ chi phí"])

NAMES = {
    "route": "Tuyến", "shipments": "Số lô", "miles": "Dặm", "revenue": "Doanh thu", "fuel_cost": "Nhiên liệu",
    "maint_cost": "Bảo trì (phân bổ)", "total_cost": "Tổng chi phí", "profit": "Lợi nhuận", "margin_pct": "Biên LN (%)",
    "rev_per_mile": "Doanh thu/dặm", "cost_per_mile": "Chi phí/dặm", "profit_per_mile": "LN/dặm",
}
money = {k: st.column_config.NumberColumn(format="$%,.0f") for k in
         ("Doanh thu", "Nhiên liệu", "Bảo trì (phân bổ)", "Tổng chi phí", "Lợi nhuận")}
per_mile = {k: st.column_config.NumberColumn(format="$%.2f") for k in ("Doanh thu/dặm", "Chi phí/dặm", "LN/dặm")}
cfg = {**money, **per_mile, "Dặm": st.column_config.NumberColumn(format="%,.0f"),
       "Biên LN (%)": st.column_config.NumberColumn(format="%.1f")}

with tab_route:
    view = by_route.sort_values("profit", ascending=False)
    st.dataframe(view[list(NAMES)].rename(columns=NAMES), width="stretch", hide_index=True, column_config=cfg)
    st.download_button(
        "Tải CSV", view[list(NAMES)].rename(columns=NAMES).to_csv(index=False).encode("utf-8"),
        file_name="route_profitability.csv", mime="text/csv",
    )
    a, b = st.columns(2)
    a.write("10 tuyến lợi nhuận cao nhất")
    a.bar_chart(view.head(10).set_index("route")["profit"])
    b.write("10 tuyến lợi nhuận thấp nhất")
    b.bar_chart(view.tail(10).sort_values("profit").set_index("route")["profit"])
    st.write("Doanh thu và chi phí trên mỗi dặm")
    st.scatter_chart(view, x="cost_per_mile", y="rev_per_mile", size="shipments")
    st.caption("Tuyến nằm phía trên đường chéo (doanh thu/dặm lớn hơn chi phí/dặm) là tuyến có lãi.")

with tab_period:
    top = by_route.sort_values("revenue", ascending=False)["route"].head(5).tolist()
    chosen = st.multiselect("Tuyến", sorted(by_route["route"]), default=top)
    metric = st.selectbox(
        "Chỉ số", ["profit", "revenue", "total_cost", "margin_pct", "rev_per_mile", "profit_per_mile", "shipments"],
        format_func=lambda m: NAMES.get(m, m),
    )
    pr = derive(raw[raw["route"].isin(by_route["route"])])
    if chosen:
        pivot = pr[pr["route"].isin(chosen)].pivot_table(index="per", columns="route", values=metric, aggfunc="sum")
        st.line_chart(pivot)
    st.write("Chi tiết tuyến x kỳ")
    detail = pr.sort_values(["per", "profit"], ascending=[False, False]).copy()
    detail["per"] = detail["per"].dt.strftime("%Y-%m-%d")
    dn = {"per": "Kỳ", **NAMES}
    st.dataframe(detail[list(dn)].rename(columns=dn), width="stretch", hide_index=True, column_config=cfg)

with tab_cov:
    st.caption(
        "BR-05 yêu cầu doanh thu và chi phí gắn được vào đúng tuyến. Phần này cho biết mức độ gắn được."
    )
    n = float(tot["shipments"])
    unassigned = raw.loc[raw["route"] == "Chưa gán tuyến", "shipments"].sum()
    c1, c2, c3 = st.columns(3)
    c1.metric("Lô đã gán tuyến", f"{(1 - unassigned / n) * 100:.1f}%" if n else "n/a")
    c2.metric("Lô có chi phí nhiên liệu", f"{raw['with_fuel'].sum() / n * 100:.1f}%" if n else "n/a")
    if use_maint:
        c3.metric("Lô có chi phí bảo trì phân bổ", f"{raw['with_maint'].sum() / n * 100:.1f}%" if n else "n/a")
    else:
        c3.metric("Lô có chi phí bảo trì phân bổ", "đã tắt")

    try:
        fuel_cov = run_query_slow(
            f"""
            SELECT toFloat64(sum(f.total_cost)) AS total,
                   toFloat64(sumIf(f.total_cost, f.trip_key IN (SELECT trip_key FROM {trip_t}))) AS attributed
            FROM {fuel_t} AS f
            INNER JOIN {date_t} AS dd ON f.date_key = dd.date_key
            WHERE dd.full_date BETWEEN toDate('{d0}') AND toDate('{d1}')
            """,
            creds,
        )
        fuel_cov = ensure_cols(fuel_cov, ["total", "attributed"])
        total_f = float(fuel_cov["total"].iloc[0] or 0) if len(fuel_cov) else 0.0
        attr_f = float(fuel_cov["attributed"].iloc[0] or 0) if len(fuel_cov) else 0.0
        st.write("Chi phí nhiên liệu trong khoảng ngày")
        d1c, d2c, d3c = st.columns(3)
        d1c.metric("Tổng mua nhiên liệu", f"${total_f:,.0f}")
        d2c.metric("Gán được vào chuyến", f"${attr_f:,.0f}")
        d3c.metric("Chưa gán vào chuyến nào", f"${total_f - attr_f:,.0f}")
        st.caption("Phần chưa gán không nằm trong lợi nhuận theo tuyến, nên lợi nhuận có thể cao hơn thực tế.")
    except Exception as e:
        st.warning("Không tính được độ phủ nhiên liệu.")
        st.caption(str(e)[:300])

    st.write("Chi phí không có trong dữ liệu và không được tính: lương tài xế, phí cầu đường, bảo hiểm, khấu hao xe.")
