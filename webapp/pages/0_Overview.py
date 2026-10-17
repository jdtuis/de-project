"""Tổng quan: các chỉ số chính của cả 8 yêu cầu nghiệp vụ trong một màn hình.
Mỗi chỉ số dẫn tới trang chi tiết tương ứng. Mặc định hiển thị tháng đã đủ ngày gần nhất."""

import math

import numpy as np
import pandas as pd
import streamlit as st

from common import ensure_cols, get_creds, month_is_partial, qi, resolve_table, run_query_slow

creds = get_creds()
DB = creds[3]

st.title("Tổng quan vận hành")

trip_t, _ = resolve_table(creds, "fact_trip")
del_t, _ = resolve_table(creds, "fact_delivery_event")
date_t, _ = resolve_table(creds, "dim_date")
safety_t, _ = resolve_table(creds, "fact_safety_incident", required=False)

# ---------------------------------------------------------------------------
# Dữ liệu theo tháng
# ---------------------------------------------------------------------------
try:
    trips = run_query_slow(
        f"""SELECT toStartOfMonth(dd.full_date) AS mth, uniqExact(t.load_id) AS loads,
                   toFloat64(sum(t.total_revenue)) AS revenue, toFloat64(sum(t.actual_miles)) AS miles
            FROM {trip_t} AS t INNER JOIN {date_t} AS dd ON t.date_key = dd.date_key GROUP BY mth ORDER BY mth""",
        creds,
    )
    deliveries = run_query_slow(
        f"""SELECT toStartOfMonth(toDate(scheduled_datetime)) AS mth,
                   countIf(isNotNull(actual_datetime)) AS delivered,
                   countIf(isNotNull(actual_datetime) AND is_on_time = 1) AS on_time
            FROM {del_t} WHERE positionCaseInsensitive(event_type, 'deliver') > 0 GROUP BY mth ORDER BY mth""",
        creds,
    )
    fresh = run_query_slow(
        f"""SELECT (SELECT max(dd.full_date) FROM {trip_t} AS t INNER JOIN {date_t} AS dd ON t.date_key = dd.date_key) AS last_trip,
                   (SELECT max(actual_datetime) FROM {del_t}) AS last_event""",
        creds,
    )
except Exception as e:
    st.error("Không đọc được dữ liệu tổng quan.")
    st.code(str(e))
    st.stop()

trips = ensure_cols(trips, ["mth", "loads", "revenue", "miles"])
deliveries = ensure_cols(deliveries, ["mth", "delivered", "on_time"])
fresh = ensure_cols(fresh, ["last_trip", "last_event"])
if trips.empty:
    st.warning("Chưa có dữ liệu chuyến đi.")
    st.stop()
trips["mth"] = pd.to_datetime(trips["mth"])
deliveries["mth"] = pd.to_datetime(deliveries["mth"])
trips = trips.set_index("mth")
deliveries = deliveries.set_index("mth")

max_date = pd.to_datetime(fresh["last_trip"].iloc[0]) if len(fresh) else None
months = list(reversed(trips.index.tolist()))
default_i = 1 if len(months) > 1 and month_is_partial(months[0], max_date) else 0
M = pd.Timestamp(st.selectbox(
    "Tháng báo cáo", months, index=default_i,
    format_func=lambda m: pd.Timestamp(m).strftime("%m/%Y") + (" (chưa đủ tháng)" if month_is_partial(m, max_date) else ""),
))
P = M - pd.DateOffset(months=1)  # tháng liền trước


def val(df, month, col):
    return float(df.loc[month, col]) if month in df.index and pd.notna(df.loc[month, col]) else np.nan


def pct_change(cur, prev):
    return f"{(cur - prev) / prev * 100:+.1f}%" if prev and not np.isnan(prev) and not np.isnan(cur) else None


# ---------------------------------------------------------------------------
# Hàng 1: vận hành và doanh thu
# ---------------------------------------------------------------------------
loads, p_loads = val(trips, M, "loads"), val(trips, P, "loads")
rev, p_rev = val(trips, M, "revenue"), val(trips, P, "revenue")
miles = val(trips, M, "miles")
p_miles = val(trips, P, "miles")
dl, ot = val(deliveries, M, "delivered"), val(deliveries, M, "on_time")
p_dl, p_ot = val(deliveries, P, "delivered"), val(deliveries, P, "on_time")
ontime = ot / dl * 100 if dl else np.nan
p_ontime = p_ot / p_dl * 100 if p_dl else np.nan

st.subheader("Vận hành và doanh thu")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Giao đúng hạn", f"{ontime:.1f}%" if not np.isnan(ontime) else "n/a",
          delta=f"{ontime - p_ontime:+.1f} điểm" if not (np.isnan(ontime) or np.isnan(p_ontime)) else None)
c2.metric("Lô hàng", f"{int(loads):,}" if not np.isnan(loads) else "n/a", delta=pct_change(loads, p_loads))
c3.metric("Doanh thu", f"${rev:,.0f}" if not np.isnan(rev) else "n/a", delta=pct_change(rev, p_rev))
c4.metric("Doanh thu / dặm", f"${rev / miles:.2f}" if miles else "n/a",
          delta=pct_change(rev / miles, p_rev / p_miles) if miles and p_miles else None)

# ---------------------------------------------------------------------------
# Hàng 2: khách hàng và an toàn
# ---------------------------------------------------------------------------
top1 = top5 = top20 = np.nan
try:
    cust = ensure_cols(run_query_slow(
        f"""SELECT t.customer_key AS ck, toFloat64(sum(t.total_revenue)) AS revenue
            FROM {trip_t} AS t INNER JOIN {date_t} AS dd ON t.date_key = dd.date_key
            WHERE toStartOfMonth(dd.full_date) = toDate('{M:%Y-%m-%d}') GROUP BY ck HAVING revenue > 0""",
        creds), ["ck", "revenue"])
    s = cust["revenue"].sort_values(ascending=False)
    if len(s) and s.sum() > 0:
        top1 = s.iloc[0] / s.sum() * 100
        top5 = s.head(5).sum() / s.sum() * 100
        top20 = s.head(max(1, math.ceil(0.2 * len(s)))).sum() / s.sum() * 100
except Exception:
    pass

inc = prev_inc = prevent = np.nan
if safety_t:
    try:
        sf = ensure_cols(run_query_slow(
            f"""SELECT toStartOfMonth(dd.full_date) AS mth, count() AS incidents, sum(s.preventable_flag) AS preventable
                FROM {safety_t} AS s INNER JOIN {date_t} AS dd ON s.date_key = dd.date_key GROUP BY mth""",
            creds), ["mth", "incidents", "preventable"])
        sf["mth"] = pd.to_datetime(sf["mth"])
        sf = sf.set_index("mth")
        inc, prev_inc, prevent = val(sf, M, "incidents"), val(sf, P, "incidents"), val(sf, M, "preventable")
        inc, prev_inc, prevent = (0 if np.isnan(x) else x for x in (inc, prev_inc, prevent))
    except Exception:
        pass

st.subheader("Khách hàng và an toàn")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Khách hàng lớn nhất", f"{top1:.1f}%" if not np.isnan(top1) else "n/a", help="Tỷ trọng doanh thu của khách hàng lớn nhất.")
c2.metric("20% khách hàng đầu", f"{top20:.1f}%" if not np.isnan(top20) else "n/a",
          help="Tỷ trọng doanh thu của nhóm 20% khách hàng có doanh thu cao nhất.")
c3.metric("Sự cố an toàn", f"{int(inc):,}" if not np.isnan(inc) else "n/a",
          delta=(f"{int(inc - prev_inc):+d} so với tháng trước" if not np.isnan(inc) else None), delta_color="inverse")
c4.metric("Phòng tránh được", f"{prevent / inc * 100:.0f}%" if inc else "n/a",
          help="Tỷ lệ sự cố có thể phòng tránh trong tháng.")

# ---------------------------------------------------------------------------
# Hàng 3: dữ liệu
# ---------------------------------------------------------------------------
dq = None
try:
    dq = run_query_slow(
        f"""SELECT run_id, min(run_ts) AS ts, countIf(status = 'PASS') AS passed, countIf(status = 'FAIL') AS failed
            FROM {qi(DB)}.dq_log GROUP BY run_id ORDER BY ts DESC LIMIT 1""",
        creds)
except Exception:
    dq = None

st.subheader("Dữ liệu")
c1, c2, c3 = st.columns(3)
last_trip = pd.to_datetime(fresh["last_trip"].iloc[0]) if len(fresh) else pd.NaT
last_event = pd.to_datetime(fresh["last_event"].iloc[0], utc=True) if len(fresh) and pd.notna(fresh["last_event"].iloc[0]) else pd.NaT
c1.metric("Chuyến đi mới nhất", f"{last_trip:%d/%m/%Y}" if pd.notna(last_trip) else "n/a")
c2.metric("Sự kiện giao hàng mới nhất (UTC)", f"{last_event:%d/%m/%Y %H:%M}" if pd.notna(last_event) else "n/a")
if dq is not None and "passed" in dq.columns and len(dq):
    r = dq.iloc[0]
    c3.metric("Kiểm tra chất lượng gần nhất", f"{int(r['failed'])} quy tắc FAIL", delta=f"{int(r['passed'])} PASS", delta_color="off")
else:
    c3.metric("Kiểm tra chất lượng gần nhất", "Chưa chạy", help="Chạy ở trang Chất lượng dữ liệu và ghi log để hiển thị ở đây.")

# ---------------------------------------------------------------------------
# Xu hướng và lối tắt
# ---------------------------------------------------------------------------
st.subheader("Xu hướng theo tháng")
a, b = st.columns(2)
a.write("Doanh thu ($)")
a.line_chart(trips["revenue"])
trend = (deliveries["on_time"] / deliveries["delivered"].where(deliveries["delivered"] > 0) * 100).rename("Giao đúng hạn (%)")
b.write("Giao đúng hạn (%)")
b.line_chart(trend)

st.subheader("Đi tới")
l1, l2, l3, l4 = st.columns(4)
l1.page_link("pages/2_Shipment_Alerts.py", label="Cảnh báo trễ hạn", icon=":material/notifications_active:")
l2.page_link("pages/1_Fleet_Performance.py", label="Hiệu suất đội xe", icon=":material/local_shipping:")
l3.page_link("pages/5_Route_Profitability.py", label="Lợi nhuận theo tuyến", icon=":material/alt_route:")
l4.page_link("pages/9_BR_Coverage.py", label="Yêu cầu nghiệp vụ", icon=":material/fact_check:")
