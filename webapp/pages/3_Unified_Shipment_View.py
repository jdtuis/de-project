"""BR-03: Unified Operational Data View.

Một dòng = một lô hàng (một chuyến trong v_fact_trip), đã nối sẵn khách hàng, tuyến, xe, tài xế,
cơ sở, sự kiện giao hàng và chi phí nhiên liệu. Người dùng không cần tự viết JOIN.
Câu SQL tạo view này nằm ở tab "Mô hình dữ liệu", có thể chạy một lần để tạo dwh.v_shipment.
"""

from datetime import date

import pandas as pd
import streamlit as st

from common import get_creds, pick, qi, resolve_table, run_query, run_query_slow, table_columns

st.set_page_config(page_title="Unified shipment view", layout="wide")
creds = get_creds()
DB = creds[3]

st.title("Unified shipment view")
st.caption(
    "Mỗi dòng là một lô hàng, đã nối sẵn khách hàng, tuyến, xe, tài xế, cơ sở, giao hàng và chi phí nhiên liệu."
)

trip_t, _ = resolve_table(creds, "fact_trip")
del_t, _ = resolve_table(creds, "fact_delivery_event")
fuel_t, _ = resolve_table(creds, "fact_fuel_purchase")
date_t, _ = resolve_table(creds, "dim_date")
cust_t, _ = resolve_table(creds, "dim_customer")
truck_t, _ = resolve_table(creds, "dim_truck")
driver_t, _ = resolve_table(creds, "dim_driver")
fac_t, _ = resolve_table(creds, "dim_facility")
route_t, route_n = resolve_table(creds, "dim_route")

# Nhãn tuyến: dò cột của dim_route vì tôi chưa biết tên cột chính xác
rc = table_columns(creds, route_n)
o_city, d_city = pick(rc, "origin_city", "origin"), pick(rc, "destination_city", "destination")
o_st, d_st = pick(rc, "origin_state"), pick(rc, "destination_state")
if o_city and d_city:
    left = f"toString({qi(o_city)})" + (f", ', ', toString({qi(o_st)})" if o_st else "")
    right = f"toString({qi(d_city)})" + (f", ', ', toString({qi(d_st)})" if d_st else "")
    ROUTE_EXPR = f"concat({left}, ' -> ', {right})"
elif pick(rc, "route_id"):
    ROUTE_EXPR = f"toString({qi(pick(rc, 'route_id'))})"
else:
    ROUTE_EXPR = "toString(route_key)"

IS_DEL = "positionCaseInsensitive(event_type, 'deliver') > 0"
IS_PICK = "positionCaseInsensitive(event_type, 'pick') > 0"

# ---------------------------------------------------------------------------
# Mô hình lô hàng: một dòng một chuyến
# ---------------------------------------------------------------------------
SHIPMENT = f"""
SELECT
    t.trip_key AS trip_key,
    t.trip_id AS trip_id,
    t.load_id AS load_id,
    dd.full_date AS ship_date,
    t.booking_type AS booking_type,
    t.load_type AS load_type,
    t.load_status AS load_status,
    cu.cn AS customer,
    cu.ct AS customer_type,
    rt.rl AS route,
    tk.un AS truck,
    dr.dn AS driver,
    fo.fn AS origin_facility,
    fd.fn AS dest_facility,
    t.weight_lbs AS weight_lbs,
    t.pieces AS pieces,
    t.planned_miles AS planned_miles,
    t.actual_miles AS actual_miles,
    toFloat64(t.trip_duration_hours) AS trip_duration_hours,
    toFloat64(t.revenue) AS revenue,
    toFloat64(t.fuel_surcharge) AS fuel_surcharge,
    toFloat64(t.accessorial_charges) AS accessorial_charges,
    toFloat64(t.total_revenue) AS total_revenue,
    toFloat64(t.fuel_consumed_gallons) AS fuel_gallons,
    toFloat64(t.idle_hours) AS idle_hours,
    ev.sd AS scheduled_delivery,
    ev.ad AS actual_delivery,
    ev.dl AS delay_minutes,
    ev.dt AS detention_minutes,
    fu.fc AS fuel_cost,
    multiIf(
        ev.has_del = 0, 'No delivery event',
        ev.delivered = 0, 'Not delivered',
        ev.on_time = 1, 'On time',
        'Late') AS delivery_status,
    if(t.actual_miles > 0, toFloat64(t.total_revenue) / t.actual_miles, NULL) AS revenue_per_mile,
    toFloat64(t.total_revenue) - ifNull(fu.fc, 0) AS revenue_less_fuel
FROM {trip_t} AS t
LEFT JOIN {date_t} AS dd ON t.date_key = dd.date_key
LEFT JOIN (SELECT customer_key AS k, any(customer_name) AS cn, any(customer_type) AS ct
           FROM {cust_t} GROUP BY k) AS cu ON t.customer_key = cu.k
LEFT JOIN (SELECT route_key AS k, any({ROUTE_EXPR}) AS rl FROM {route_t} GROUP BY k) AS rt ON t.route_key = rt.k
LEFT JOIN (SELECT truck_key AS k, any(unit_number) AS un FROM {truck_t} GROUP BY k) AS tk ON t.truck_key = tk.k
LEFT JOIN (SELECT driver_key AS k, any(driver_name) AS dn FROM {driver_t} GROUP BY k) AS dr ON t.driver_key = dr.k
LEFT JOIN (
    SELECT trip_key AS k,
           max({IS_DEL}) AS has_del,
           max({IS_DEL} AND isNotNull(actual_datetime)) AS delivered,
           maxIf(toNullable(scheduled_datetime), {IS_DEL}) AS sd,
           maxIf(toNullable(actual_datetime), {IS_DEL}) AS ad,
           maxIf(toNullable(delay_minutes), {IS_DEL}) AS dl,
           sum(detention_minutes) AS dt,
           minIf(toNullable(is_on_time), {IS_DEL} AND isNotNull(actual_datetime)) AS on_time,
           anyIf(toNullable(facility_key), {IS_PICK}) AS fok,
           anyIf(toNullable(facility_key), {IS_DEL}) AS fdk
    FROM {del_t} GROUP BY k
) AS ev ON t.trip_key = ev.k
LEFT JOIN (SELECT facility_key AS k, any(facility_name) AS fn FROM {fac_t} GROUP BY k) AS fo ON ev.fok = fo.k
LEFT JOIN (SELECT facility_key AS k, any(facility_name) AS fn FROM {fac_t} GROUP BY k) AS fd ON ev.fdk = fd.k
LEFT JOIN (SELECT trip_key AS k, toFloat64(sum(total_cost)) AS fc FROM {fuel_t} GROUP BY k) AS fu ON t.trip_key = fu.k
"""


def sq(select: str, tail: str = "") -> str:
    """Truy vấn trên mô hình lô hàng. join_use_nulls để lô thiếu dữ liệu nối ra NULL thay vì giá trị mặc định."""
    return f"SELECT {select} FROM ({SHIPMENT}) AS s {tail} SETTINGS join_use_nulls = 1"


def lit(x: str) -> str:
    return "'" + str(x).replace("\\", "\\\\").replace("'", "\\'") + "'"


# ---------------------------------------------------------------------------
# Bộ lọc
# ---------------------------------------------------------------------------
try:
    bounds = run_query_slow(sq("min(ship_date) AS lo, max(ship_date) AS hi"), creds)
    customers = run_query_slow(
        sq("customer AS c, count() AS n", "WHERE customer IS NOT NULL GROUP BY c ORDER BY n DESC LIMIT 500"), creds
    )
except Exception as e:
    st.error("Không dựng được mô hình lô hàng. Có thể tên cột trong các bảng dim/fact khác với dự kiến.")
    st.code(str(e))
    st.stop()

lo = pd.to_datetime(bounds["lo"].iloc[0]).date() if len(bounds) and pd.notna(bounds["lo"].iloc[0]) else date(2000, 1, 1)
hi = pd.to_datetime(bounds["hi"].iloc[0]).date() if len(bounds) and pd.notna(bounds["hi"].iloc[0]) else date.today()

st.sidebar.divider()
st.sidebar.subheader("Bộ lọc")
rng = st.sidebar.date_input("Ngày chạy", value=(lo, hi), min_value=lo, max_value=hi)
sel_customers = st.sidebar.multiselect("Khách hàng", customers["c"].tolist() if "c" in customers else [])
sel_status = st.sidebar.multiselect(
    "Trạng thái giao hàng", ["On time", "Late", "Not delivered", "No delivery event"]
)
search = st.sidebar.text_input("Tìm load_id hoặc trip_id")

conds = []
if isinstance(rng, (tuple, list)) and len(rng) == 2:
    conds.append(f"s.ship_date BETWEEN toDate({lit(rng[0].isoformat())}) AND toDate({lit(rng[1].isoformat())})")
if sel_customers:
    conds.append("s.customer IN (" + ", ".join(lit(c) for c in sel_customers) + ")")
if sel_status:
    conds.append("s.delivery_status IN (" + ", ".join(lit(c) for c in sel_status) + ")")
if search.strip():
    q = lit(search.strip())
    conds.append(f"(positionCaseInsensitive(s.load_id, {q}) > 0 OR positionCaseInsensitive(s.trip_id, {q}) > 0)")
WHERE = ("WHERE " + " AND ".join(conds)) if conds else ""
AND = (" AND " + " AND ".join(conds)) if conds else ""

# ---------------------------------------------------------------------------
# Chỉ số tổng quan
# ---------------------------------------------------------------------------
kpi = run_query(
    sq(
        """count() AS n,
           countIf(delivery_status IN ('On time', 'Late')) AS delivered,
           countIf(delivery_status = 'On time') AS on_time,
           sum(total_revenue) AS rev, sum(fuel_cost) AS fuel, sum(actual_miles) AS miles""",
        WHERE,
    ),
    creds,
)
r = kpi.iloc[0] if len(kpi) else pd.Series(dtype=float)
n = int(r.get("n", 0) or 0)
delivered = int(r.get("delivered", 0) or 0)
rev = float(r.get("rev", 0) or 0)
miles = float(r.get("miles", 0) or 0)
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Lô hàng", f"{n:,}")
k2.metric("Giao đúng hạn", f"{int(r.get('on_time', 0) or 0) / delivered * 100:.1f}%" if delivered else "n/a")
k3.metric("Tổng doanh thu", f"${rev:,.0f}")
k4.metric("Doanh thu / dặm", f"${rev / miles:,.2f}" if miles else "n/a")
k5.metric("Chi phí nhiên liệu", f"${float(r.get('fuel', 0) or 0):,.0f}")

tab_list, tab_q, tab_model = st.tabs(["Danh sách lô hàng", "Câu hỏi chuẩn", "Mô hình dữ liệu"])

# ---------------------------------------------------------------------------
# Danh sách
# ---------------------------------------------------------------------------
with tab_list:
    limit = st.number_input("Số dòng hiển thị", 50, 20000, 1000, step=50)
    data = run_query(sq("*", f"{WHERE} ORDER BY s.ship_date DESC, s.trip_id LIMIT {int(limit)}"), creds)
    st.dataframe(data, width="stretch", hide_index=True)
    st.download_button(
        "Tải CSV", data.to_csv(index=False).encode("utf-8"), file_name="shipments.csv", mime="text/csv"
    )

# ---------------------------------------------------------------------------
# Câu hỏi chuẩn: mỗi câu chỉ cần truy vấn một nguồn là mô hình lô hàng, không JOIN thêm
# ---------------------------------------------------------------------------
with tab_q:
    st.caption("Mỗi câu hỏi dưới đây được trả lời chỉ bằng một truy vấn trên mô hình lô hàng, không cần JOIN thêm bảng nào.")
    QUESTIONS = {
        "Lô hàng này có giao đúng hạn không?": dict(
            needs_id=True,
            sql=lambda x: sq(
                "load_id, trip_id, customer, route, scheduled_delivery, actual_delivery, delay_minutes, delivery_status",
                f"WHERE load_id = {lit(x)} OR trip_id = {lit(x)}",
            ),
        ),
        "Lô hàng này mang lại bao nhiêu doanh thu và tốn bao nhiêu nhiên liệu?": dict(
            needs_id=True,
            sql=lambda x: sq(
                "load_id, trip_id, total_revenue, fuel_cost, revenue_less_fuel, actual_miles, revenue_per_mile",
                f"WHERE load_id = {lit(x)} OR trip_id = {lit(x)}",
            ),
        ),
        "Tỷ lệ giao đúng hạn theo khách hàng": dict(
            sql=lambda x: sq(
                """customer, count() AS shipments,
                   countIf(delivery_status IN ('On time', 'Late')) AS delivered,
                   round(100 * countIf(delivery_status = 'On time') / nullIf(countIf(delivery_status IN ('On time', 'Late')), 0), 1) AS on_time_pct""",
                f"{WHERE} GROUP BY customer ORDER BY shipments DESC LIMIT 20",
            ),
        ),
        "Doanh thu và doanh thu trên dặm theo tuyến": dict(
            sql=lambda x: sq(
                """route, count() AS shipments, round(sum(total_revenue)) AS revenue,
                   round(sum(total_revenue) / nullIf(sum(actual_miles), 0), 2) AS revenue_per_mile""",
                f"{WHERE} GROUP BY route ORDER BY revenue DESC LIMIT 20",
            ),
        ),
        "10 lô hàng trễ nhiều nhất": dict(
            sql=lambda x: sq(
                "load_id, customer, route, scheduled_delivery, actual_delivery, delay_minutes",
                f"WHERE delivery_status = 'Late'{AND} ORDER BY delay_minutes DESC LIMIT 10",
            ),
        ),
        "Các lô hàng chưa giao": dict(
            sql=lambda x: sq(
                "load_id, customer, route, scheduled_delivery, ship_date",
                f"WHERE delivery_status = 'Not delivered'{AND} ORDER BY scheduled_delivery LIMIT 100",
            ),
        ),
        "Thời gian chờ (detention) trung bình theo cơ sở giao hàng": dict(
            sql=lambda x: sq(
                "dest_facility, count() AS shipments, round(avg(detention_minutes), 1) AS avg_detention_minutes",
                f"{WHERE} GROUP BY dest_facility ORDER BY avg_detention_minutes DESC LIMIT 20",
            ),
        ),
        "Doanh thu theo loại tải và loại booking": dict(
            sql=lambda x: sq(
                "load_type, booking_type, count() AS shipments, round(sum(total_revenue)) AS revenue",
                f"{WHERE} GROUP BY load_type, booking_type ORDER BY revenue DESC",
            ),
        ),
    }
    choice = st.selectbox("Chọn câu hỏi", list(QUESTIONS))
    spec = QUESTIONS[choice]
    ident = ""
    if spec.get("needs_id"):
        ident = st.text_input("Nhập load_id hoặc trip_id")
    if spec.get("needs_id") and not ident.strip():
        st.info("Nhập mã lô hàng để xem kết quả.")
    else:
        sql = spec["sql"](ident.strip())
        try:
            res = run_query(sql, creds)
        except Exception as e:
            st.error("Truy vấn lỗi.")
            st.code(str(e))
            res = None
        if res is not None:
            if res.empty:
                st.info("Không có kết quả.")
            else:
                st.dataframe(res, width="stretch", hide_index=True)
                nums = [c for c in res.columns[1:] if pd.api.types.is_numeric_dtype(res[c])]
                if len(res) > 1 and nums:
                    st.bar_chart(res.set_index(res.columns[0])[nums[-1]])
            with st.expander("Xem câu SQL"):
                st.code(sql, language="sql")

# ---------------------------------------------------------------------------
# Mô hình dữ liệu
# ---------------------------------------------------------------------------
with tab_model:
    st.write(
        "Mô hình lô hàng lấy `v_fact_trip` làm gốc (một dòng một chuyến) và nối thêm: ngày (`dim_date`), khách hàng, "
        "tuyến, xe, tài xế, cơ sở nhận và giao, sự kiện giao hàng (tổng hợp theo chuyến) và chi phí nhiên liệu "
        "(`v_fact_fuel_purchase`)."
    )
    st.write(
        "Muốn các công cụ khác (Superset, DBeaver) dùng chung mô hình này, chạy câu dưới đây một lần trong ClickHouse để tạo view."
    )
    create_sql = f"CREATE OR REPLACE VIEW {qi(DB)}.v_shipment AS\n{SHIPMENT.strip()}"
    st.code(create_sql, language="sql")
    st.download_button("Tải v_shipment.sql", create_sql.encode("utf-8"), file_name="v_shipment.sql", mime="text/plain")
    st.caption(
        "Khi truy vấn view này nên thêm SETTINGS join_use_nulls = 1 để lô thiếu dữ liệu nối ra NULL thay vì giá trị mặc định."
    )
    st.write("Cột của mô hình:")
    desc = run_query(f"DESCRIBE TABLE ({SHIPMENT}) SETTINGS join_use_nulls = 1", creds)
    if {"name", "type"} <= set(desc.columns):
        st.dataframe(desc[["name", "type"]], width="stretch", hide_index=True)
