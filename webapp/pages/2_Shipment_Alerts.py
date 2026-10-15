"""BR-02: Real-Time Shipment Tracking.

Nguồn: v_fact_delivery_event (sự kiện giao hàng, giờ UTC).
  - At risk : chưa giao và hạn giao sắp đến (trong N phút tới)
  - Breached: giao trễ so với hạn, hoặc đã quá hạn mà vẫn chưa giao
Kèm số phút trễ / số phút còn lại. Xe, tài xế, cơ sở, khách hàng lấy từ các bảng dim.
"""

from datetime import datetime, time, timedelta, timezone

import numpy as np
import pandas as pd
import streamlit as st

from common import ensure_cols, get_creds, resolve_table, run_query

st.set_page_config(page_title="Shipment alerts", layout="wide")
creds = get_creds()

st.title("Shipment alerts")
st.caption("Cảnh báo lô hàng có nguy cơ hoặc đã trễ hạn giao, để điều phối can thiệp sớm. Mọi mốc thời gian theo UTC.")

de_t, _ = resolve_table(creds, "fact_delivery_event")
trip_t, _ = resolve_table(creds, "fact_trip")
truck_t, _ = resolve_table(creds, "dim_truck")
driver_t, _ = resolve_table(creds, "dim_driver")
fac_t, _ = resolve_table(creds, "dim_facility")
cust_t, _ = resolve_table(creds, "dim_customer")

EVENT_FILTER = "positionCaseInsensitive(event_type, 'deliver') > 0"

# Mốc dữ liệu mới nhất, dùng làm giá trị mặc định cho chế độ phát lại
try:
    latest_all = run_query(
        f"SELECT max(actual_datetime) AS m FROM {de_t} WHERE {EVENT_FILTER}", creds
    )["m"].iloc[0]
except Exception as e:
    st.error("Không đọc được bảng sự kiện giao hàng.")
    st.code(str(e))
    st.stop()
latest_all = pd.to_datetime(latest_all, utc=True)

# ---------------------------------------------------------------------------
# Tham số
# ---------------------------------------------------------------------------
st.sidebar.divider()
st.sidebar.subheader("Tham số")
mode = st.sidebar.radio(
    "Thời điểm tham chiếu",
    ["Thời gian thực", "Mốc mới nhất trong dữ liệu", "Phát lại tại thời điểm chọn"],
    help="Dữ liệu lịch sử không có lô nào đang chưa giao ở hiện tại. Dùng 'Phát lại' để xem cảnh báo tại một thời điểm trong quá khứ.",
)
replay_at = None
if mode == "Phát lại tại thời điểm chọn":
    default_day = (latest_all - pd.Timedelta(days=1)).date() if pd.notna(latest_all) else datetime(2024, 1, 15).date()
    d0 = st.sidebar.date_input("Ngày (UTC)", value=default_day)
    t0 = st.sidebar.time_input("Giờ (UTC)", value=time(12, 0))
    replay_at = datetime.combine(d0, t0)

window_min = st.sidebar.slider("Cảnh báo 'At risk' trước hạn (phút)", 15, 480, 120, 15)
grace_min = st.sidebar.number_input("Dung sai trễ (phút)", 0, 120, 0)
lookback_label = st.sidebar.selectbox("Xem lại", ["6 giờ", "24 giờ", "7 ngày", "30 ngày", "Tất cả"], index=1)
lookback_h = {"6 giờ": 6, "24 giờ": 24, "7 ngày": 168, "30 ngày": 720, "Tất cả": None}[lookback_label]
auto = st.sidebar.toggle("Tự làm mới (30 giây)", value=(mode == "Thời gian thực"))

with st.sidebar.expander("Giá trị event_type trong dữ liệu"):
    st.dataframe(
        run_query(f"SELECT event_type, count() AS n FROM {de_t} GROUP BY event_type ORDER BY n DESC", creds),
        hide_index=True,
    )
    st.caption("Chỉ các loại có chữ 'deliver' được tính là sự kiện giao hàng.")


# ---------------------------------------------------------------------------
# Thân trang (chạy lại mỗi 30 giây nếu bật tự làm mới)
# ---------------------------------------------------------------------------
def render():
    latest_df = run_query(f"SELECT max(actual_datetime) AS m FROM {de_t} WHERE {EVENT_FILTER}", creds)
    latest = pd.to_datetime(latest_df["m"].iloc[0], utc=True)

    if mode == "Thời gian thực":
        ref = datetime.now(timezone.utc).replace(tzinfo=None)
    elif mode == "Mốc mới nhất trong dữ liệu":
        if pd.isna(latest):
            st.warning("Không tìm thấy sự kiện giao hàng nào có thời gian thực tế.")
            return
        ref = latest.tz_localize(None).to_pydatetime()
    else:
        ref = replay_at
    ref = ref.replace(microsecond=0)
    REF = f"toDateTime('{ref:%Y-%m-%d %H:%M:%S}', 'UTC')"
    g = int(grace_min)

    # D: sự kiện giao hàng trong cửa sổ quan tâm. act chỉ có giá trị nếu đã xảy ra trước mốc tham chiếu
    # (để phát lại lịch sử đúng thời điểm).
    lower = f"AND scheduled_datetime >= {REF} - INTERVAL {lookback_h} HOUR" if lookback_h else ""
    D = f"""(
        SELECT event_id, load_id, trip_key, facility_key,
               scheduled_datetime AS sched,
               if(actual_datetime <= {REF}, actual_datetime, NULL) AS act
        FROM {de_t}
        WHERE {EVENT_FILTER}
          AND scheduled_datetime <= {REF} + INTERVAL {int(window_min)} MINUTE {lower}
    )"""

    summary_df = run_query(
        f"""
        SELECT countIf(isNotNull(act)) AS delivered,
               countIf(ifNull(act > sched + INTERVAL {g} MINUTE, 0)) AS late,
               countIf(isNull(act) AND sched + INTERVAL {g} MINUTE < {REF}) AS overdue,
               countIf(isNull(act) AND sched + INTERVAL {g} MINUTE >= {REF}) AS at_risk
        FROM {D} AS d
        """,
        creds,
    )
    summary = ensure_cols(summary_df, ["delivered", "late", "overdue", "at_risk"]).fillna(0)
    summary = summary.iloc[0] if len(summary) else pd.Series({"delivered": 0, "late": 0, "overdue": 0, "at_risk": 0})

    def feed_sql(with_joins: bool) -> str:
        if with_joins:
            inner = f"""
            SELECT d.*, tt.un AS truck, dr.dn AS driver, fa.fn AS facility, cu.cn AS customer
            FROM {D} AS d
            LEFT JOIN (SELECT trip_key AS trk, any(truck_key) AS tkey, any(driver_key) AS dkey,
                              any(customer_key) AS ckey
                       FROM {trip_t} GROUP BY trk) AS tr ON d.trip_key = tr.trk
            LEFT JOIN (SELECT truck_key AS k, any(unit_number) AS un FROM {truck_t} GROUP BY k) AS tt ON tr.tkey = tt.k
            LEFT JOIN (SELECT driver_key AS k, any(driver_name) AS dn FROM {driver_t} GROUP BY k) AS dr ON tr.dkey = dr.k
            LEFT JOIN (SELECT facility_key AS k, any(facility_name) AS fn FROM {fac_t} GROUP BY k) AS fa ON d.facility_key = fa.k
            LEFT JOIN (SELECT customer_key AS k, any(customer_name) AS cn FROM {cust_t} GROUP BY k) AS cu ON tr.ckey = cu.k
            """
        else:
            inner = f"SELECT d.* FROM {D} AS d"
        return f"""
        SELECT * FROM (
            SELECT x.*,
                   multiIf(
                       ifNull(x.act > x.sched + INTERVAL {g} MINUTE, 0), 'Breached: giao trễ',
                       isNull(x.act) AND x.sched + INTERVAL {g} MINUTE < {REF}, 'Breached: quá hạn chưa giao',
                       isNull(x.act), 'At risk',
                       'OK') AS status
            FROM ({inner}) AS x
        )
        WHERE status != 'OK'
        ORDER BY status, sched DESC
        LIMIT 3000
        """

    try:
        feed = run_query(feed_sql(True), creds)
    except Exception as e:
        st.warning("Không nối được các bảng dim (xe, tài xế, cơ sở, khách hàng), hiển thị dữ liệu cơ bản.")
        st.caption(str(e)[:300])
        feed = run_query(feed_sql(False), creds)

    # Tính số phút bằng pandas (feed có thể rỗng và không có tên cột nếu không có cảnh báo nào)
    feed = ensure_cols(
        feed,
        ["event_id", "load_id", "trip_key", "facility_key", "sched", "act", "status",
         "truck", "driver", "facility", "customer"],
    )
    for c in ("sched", "act"):
        feed[c] = pd.to_datetime(feed[c], utc=True, errors="coerce")
    ref_ts = pd.Timestamp(ref, tz="UTC")
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
    st.caption(f"Mốc tham chiếu: {ref:%d/%m/%Y %H:%M:%S} UTC. Cửa sổ xem lại: {lookback_label}.")
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
            "Sự kiện giao mới nhất (UTC)",
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
        "load_id": "Load", "truck": "Xe", "driver": "Tài xế",
        "facility": "Cơ sở", "customer": "Khách hàng", "event_id": "Event",
    }

    def view(df, order, ascending):
        cols = [c for c in names if c in df.columns]
        out = df.sort_values(order, ascending=ascending)[cols].rename(columns=names)
        # bỏ các cột rỗng hoàn toàn
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
            br_view = view(br, "delay_min", False)
            st.dataframe(br_view, width="stretch", hide_index=True, column_config=num_cfg)
            order = ["<= 30", "30-60", "60-120", "120-240", "> 240"]
            buckets = pd.cut(
                br["delay_min"], [-np.inf, 30, 60, 120, 240, np.inf], labels=order,
            ).value_counts().reindex(order)
            st.write("Phân bố số phút trễ")
            st.bar_chart(buckets)
            st.download_button(
                "Tải CSV", br_view.to_csv(index=False).encode("utf-8"),
                file_name="breached_shipments.csv", mime="text/csv",
            )

    if len(feed) >= 3000:
        st.caption("Chỉ hiển thị 3.000 cảnh báo đầu tiên. Thu hẹp cửa sổ xem lại để thấy đầy đủ.")


if auto:
    st.fragment(render, run_every=30)()
else:
    render()
