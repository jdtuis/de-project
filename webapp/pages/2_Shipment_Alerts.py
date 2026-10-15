"""BR-02: Real-Time Shipment Tracking.

Nguồn: delivery_events (các sự kiện giao hàng).
  - At risk : chưa giao và hạn giao sắp đến (trong N phút tới)
  - Breached: giao trễ so với hạn, hoặc đã quá hạn mà vẫn chưa giao
Kèm số phút trễ / số phút còn lại.
"""

from datetime import datetime, time, timedelta, timezone

import numpy as np
import pandas as pd
import streamlit as st

from common import dt, get_creds, pick, qi, require, resolve_table, run_query, table_columns

st.set_page_config(page_title="Shipment alerts", layout="wide")
creds = get_creds()

st.title("Shipment alerts")
st.caption("Cảnh báo lô hàng có nguy cơ hoặc đã trễ hạn giao, để điều phối can thiệp sớm.")

# ---------------------------------------------------------------------------
# 1. Tìm bảng và cột
# ---------------------------------------------------------------------------
de_t, de_n = resolve_table(creds, "delivery_events")
dc = table_columns(creds, de_n)

col = {
    "scheduled": pick(dc, "scheduled_datetime", "scheduled_time", "scheduled_date", "scheduled_at"),
    "actual": pick(dc, "actual_datetime", "actual_time", "actual_date", "actual_at"),
}
require(col, {de_n: dc})
event_col = pick(dc, "event_type")
id_col = pick(dc, "event_id")
load_col = pick(dc, "load_id")
trip_col = pick(dc, "trip_id")
fac_col = pick(dc, "facility_id")


def ts(c):
    return f"toString({qi(c)})" if c else "''"


S = dt(col["scheduled"])
A = dt(col["actual"])
EVENT_FILTER = f"positionCaseInsensitive(toString({qi(event_col)}), 'deliver') > 0" if event_col else "1"

# Bảng tùy chọn để làm giàu thông tin (xe, tài xế, cơ sở, khách hàng). Thiếu thì bỏ qua.
joins, extras = [], []
trips_t, trips_n = resolve_table(creds, "trips", required=False)
if trips_t and trip_col:
    kc = table_columns(creds, trips_n)
    t_trip, t_truck, t_driver = pick(kc, "trip_id"), pick(kc, "truck_id"), pick(kc, "driver_id")
    if t_trip and t_truck:
        driver_sel = f", any(toString({qi(t_driver)})) AS dv" if t_driver else ", '' AS dv"
        joins.append(
            f"LEFT JOIN (SELECT toString({qi(t_trip)}) AS tr, any(toString({qi(t_truck)})) AS tk{driver_sel} "
            f"FROM {trips_t} GROUP BY tr) AS t ON d.tr = t.tr"
        )
        extras += ["t.tk AS truck", "t.dv AS driver"]

fac_t, fac_n = resolve_table(creds, "facilities", required=False)
if fac_t and fac_col:
    kc = table_columns(creds, fac_n)
    f_id, f_name = pick(kc, "facility_id"), pick(kc, "facility_name", "name")
    if f_id and f_name:
        joins.append(
            f"LEFT JOIN (SELECT toString({qi(f_id)}) AS fc, any(toString({qi(f_name)})) AS fname "
            f"FROM {fac_t} GROUP BY fc) AS f ON d.fc = f.fc"
        )
        extras.append("f.fname AS facility")

loads_t, loads_n = resolve_table(creds, "loads", required=False)
cust_t, cust_n = resolve_table(creds, "customers", required=False)
if loads_t and cust_t and load_col:
    lc, cc = table_columns(creds, loads_n), table_columns(creds, cust_n)
    l_id, l_cust = pick(lc, "load_id"), pick(lc, "customer_id")
    c_id, c_name = pick(cc, "customer_id"), pick(cc, "customer_name", "company_name", "name")
    if l_id and l_cust and c_id and c_name:
        joins.append(
            f"LEFT JOIN (SELECT toString(l.{qi(l_id)}) AS ld, any(toString(c.{qi(c_name)})) AS cname "
            f"FROM {loads_t} AS l LEFT JOIN {cust_t} AS c "
            f"ON toString(l.{qi(l_cust)}) = toString(c.{qi(c_id)}) GROUP BY ld) AS cu ON d.ld = cu.ld"
        )
        extras.append("cu.cname AS customer")

# ---------------------------------------------------------------------------
# 2. Tham số
# ---------------------------------------------------------------------------
st.sidebar.divider()
st.sidebar.subheader("Tham số")
mode = st.sidebar.radio(
    "Thời điểm tham chiếu",
    ["Thời gian thực", "Mốc mới nhất trong dữ liệu", "Phát lại tại thời điểm chọn"],
    help="Dữ liệu lịch sử không có lô nào chưa giao ở hiện tại. Hãy dùng 'Phát lại' để xem cảnh báo tại một thời điểm trong quá khứ.",
)
tz_offset = 0.0
if mode == "Thời gian thực":
    tz_offset = st.sidebar.number_input(
        "Lệch giờ so với UTC của dữ liệu (giờ)", -12.0, 14.0, 0.0, 0.5,
        help="Nếu scheduled_datetime lưu theo giờ địa phương, nhập độ lệch (ví dụ Việt Nam là 7).",
    )
replay_at = None
if mode == "Phát lại tại thời điểm chọn":
    d0 = st.sidebar.date_input("Ngày", value=datetime(2024, 1, 15).date())
    t0 = st.sidebar.time_input("Giờ", value=time(12, 0))
    replay_at = datetime.combine(d0, t0)

window_min = st.sidebar.slider("Cảnh báo 'At risk' trước hạn (phút)", 15, 480, 120, 15)
grace_min = st.sidebar.number_input("Dung sai trễ (phút)", 0, 120, 0)
lookback_label = st.sidebar.selectbox("Xem lại", ["6 giờ", "24 giờ", "7 ngày", "30 ngày", "Tất cả"], index=1)
lookback_h = {"6 giờ": 6, "24 giờ": 24, "7 ngày": 168, "30 ngày": 720, "Tất cả": None}[lookback_label]
auto = st.sidebar.toggle("Tự làm mới (30 giây)", value=(mode == "Thời gian thực"))


# ---------------------------------------------------------------------------
# 3. Thân trang (chạy lại mỗi 30 giây nếu bật tự làm mới)
# ---------------------------------------------------------------------------
def render():
    # Mốc thời gian tham chiếu
    latest_df = run_query(f"SELECT max({A}) AS m FROM {de_t} WHERE {EVENT_FILTER}", creds)
    latest = pd.to_datetime(latest_df["m"].iloc[0]) if len(latest_df) else pd.NaT

    if mode == "Thời gian thực":
        ref = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=tz_offset)
    elif mode == "Mốc mới nhất trong dữ liệu":
        if pd.isna(latest):
            st.warning("Không tìm thấy sự kiện giao hàng nào có thời gian thực tế.")
            return
        ref = latest.to_pydatetime()
    else:
        ref = replay_at
    ref = ref.replace(microsecond=0)
    REF = f"toDateTime('{ref:%Y-%m-%d %H:%M:%S}')"
    g = int(grace_min)

    # Subquery d: sự kiện giao hàng trong cửa sổ quan tâm.
    # act chỉ có giá trị nếu đã xảy ra trước mốc tham chiếu (để phát lại lịch sử đúng thời điểm).
    lower = f"AND {S} >= {REF} - INTERVAL {lookback_h} HOUR" if lookback_h else ""
    D = f"""(
        SELECT {ts(id_col)} AS ev, {ts(load_col)} AS ld, {ts(trip_col)} AS tr, {ts(fac_col)} AS fc,
               {S} AS sched,
               if({A} <= {REF}, {A}, NULL) AS act
        FROM {de_t}
        WHERE {EVENT_FILTER} AND {S} IS NOT NULL
          AND {S} <= {REF} + INTERVAL {int(window_min)} MINUTE {lower}
    )"""

    summary = run_query(
        f"""
        SELECT countIf(isNotNull(act)) AS delivered,
               countIf(ifNull(act > sched + INTERVAL {g} MINUTE, 0)) AS late,
               countIf(isNull(act) AND sched + INTERVAL {g} MINUTE < {REF}) AS overdue,
               countIf(isNull(act) AND sched + INTERVAL {g} MINUTE >= {REF}) AS at_risk
        FROM {D} AS d
        """,
        creds,
    ).iloc[0]

    def feed_sql(with_joins: bool) -> str:
        sel = ", ".join(["d.*"] + (extras if with_joins else []))
        jn = " ".join(joins if with_joins else [])
        return f"""
        SELECT * FROM (
            SELECT x.*,
                   multiIf(
                       ifNull(x.act > x.sched + INTERVAL {g} MINUTE, 0), 'Breached: giao trễ',
                       isNull(x.act) AND x.sched + INTERVAL {g} MINUTE < {REF}, 'Breached: quá hạn chưa giao',
                       isNull(x.act), 'At risk',
                       'OK') AS status
            FROM (SELECT {sel} FROM {D} AS d {jn}) AS x
        )
        WHERE status != 'OK'
        ORDER BY status, sched DESC
        LIMIT 3000
        """

    try:
        feed = run_query(feed_sql(True), creds)
    except Exception as e:
        st.warning("Không nối được các bảng bổ sung (trips, facilities, customers), hiển thị dữ liệu cơ bản.")
        st.caption(str(e)[:300])
        feed = run_query(feed_sql(False), creds)

    # Tính số phút bằng pandas
    for c in ("sched", "act"):
        feed[c] = pd.to_datetime(feed[c], errors="coerce")
    ref_ts = pd.Timestamp(ref)
    is_late = feed["status"] == "Breached: giao trễ"
    is_over = feed["status"] == "Breached: quá hạn chưa giao"
    is_risk = feed["status"] == "At risk"
    feed["delay_min"] = np.select(
        [is_late, is_over],
        [(feed["act"] - feed["sched"]).dt.total_seconds() / 60, (ref_ts - feed["sched"]).dt.total_seconds() / 60],
        default=np.nan,
    )
    feed["mins_left"] = np.where(is_risk, (feed["sched"] - ref_ts).dt.total_seconds() / 60, np.nan)

    # --- Chỉ số tổng quan ----------------------------------------------------
    st.caption(f"Mốc tham chiếu: {ref:%d/%m/%Y %H:%M:%S}. Cửa sổ xem lại: {lookback_label}.")
    breached = int(summary["late"] + summary["overdue"])
    delivered = int(summary["delivered"])
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Breached", breached)
    k2.metric("At risk", int(summary["at_risk"]))
    k3.metric("Trễ trung bình (phút)", f"{feed['delay_min'].mean():.0f}" if feed["delay_min"].notna().any() else "n/a")
    k4.metric("Giao đúng hạn", f"{(1 - summary['late'] / delivered) * 100:.1f}%" if delivered else "n/a")
    if pd.notna(latest):
        lag = (ref_ts - latest).total_seconds() / 60 if mode == "Thời gian thực" else None
        k5.metric(
            "Sự kiện giao mới nhất",
            f"{latest:%H:%M:%S}",
            delta=(f"cách {lag:.0f} phút" if lag is not None else None),
            delta_color="off",
        )
    else:
        k5.metric("Sự kiện giao mới nhất", "n/a")

    # --- Bảng cảnh báo ---------------------------------------------------------
    names = {
        "status": "Trạng thái", "sched": "Hạn giao", "act": "Giao thực tế",
        "delay_min": "Trễ (phút)", "mins_left": "Còn lại (phút)",
        "ld": "Load", "tr": "Trip", "truck": "Xe", "driver": "Tài xế",
        "facility": "Cơ sở", "customer": "Khách hàng", "ev": "Event",
    }

    def view(df, order, ascending):
        cols = [c for c in names if c in df.columns]
        out = df.sort_values(order, ascending=ascending)[cols].rename(columns=names)
        # bỏ các cột rỗng hoàn toàn (ví dụ không có bảng bổ sung)
        return out.loc[:, ~(out.isna().all() | (out.astype(str) == "").all())]

    num_cfg = {
        "Trễ (phút)": st.column_config.NumberColumn(format="%.0f"),
        "Còn lại (phút)": st.column_config.NumberColumn(format="%.0f"),
    }
    tab_risk, tab_breach = st.tabs([f"At risk ({int(summary['at_risk'])})", f"Breached ({breached})"])

    with tab_risk:
        risk = feed[is_risk]
        if risk.empty:
            st.info(
                "Không có lô nào chưa giao mà sắp đến hạn tại mốc này. "
                "Với dữ liệu lịch sử, hãy chọn 'Phát lại tại thời điểm chọn' ở thanh bên."
            )
        else:
            st.dataframe(view(risk, "mins_left", True), width="stretch", hide_index=True, column_config=num_cfg)

    with tab_breach:
        br = feed[is_late | is_over]
        if br.empty:
            st.success("Không có lô nào trễ hạn trong cửa sổ này.")
        else:
            st.dataframe(view(br, "delay_min", False), width="stretch", hide_index=True, column_config=num_cfg)
            buckets = pd.cut(
                br["delay_min"], [-np.inf, 30, 60, 120, 240, np.inf],
                labels=["<= 30", "30-60", "60-120", "120-240", "> 240"],
            ).value_counts().reindex(["<= 30", "30-60", "60-120", "120-240", "> 240"])
            st.write("Phân bố số phút trễ")
            st.bar_chart(buckets)
            st.download_button(
                "Tải CSV", view(br, "delay_min", False).to_csv(index=False).encode("utf-8"),
                file_name="breached_shipments.csv", mime="text/csv",
            )

    if len(feed) >= 3000:
        st.caption("Chỉ hiển thị 3.000 cảnh báo đầu tiên. Thu hẹp cửa sổ xem lại để thấy đầy đủ.")


if auto:
    st.fragment(render, run_every=30)()
else:
    render()
