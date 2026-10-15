"""BR-07: Driver Safety & Risk Scorecard.

Mỗi tài xế, mỗi tháng (hoặc cửa sổ 3 tháng gần nhất):
  - số sự cố, tỷ lệ lỗi của tài xế (at-fault), tỷ lệ có thể phòng tránh (preventable), tổng chi phí thiệt hại và bồi thường
Điểm rủi ro = trung bình thứ hạng phần trăm của 4 chỉ số trên trong nhóm tài xế có sự cố.
Nhóm cao nhất (mặc định 10%) bị gắn cờ để ưu tiên huấn luyện hoặc can thiệp.

Tên cột của bảng sự cố được dò tự động và có thể đổi ở thanh bên (mục "Cột dữ liệu").
"""

import math

import numpy as np
import pandas as pd
import streamlit as st

from common import (
    ensure_cols, get_creds, is_numeric_type, month_is_partial, pick, qi, resolve_table, run_query, run_query_slow,
    table_columns, table_schema,
)

st.set_page_config(page_title="Driver safety", layout="wide")
creds = get_creds()

st.title("Driver safety scorecard")
st.caption("Điểm rủi ro an toàn của từng tài xế theo tháng, để chọn người cần huấn luyện hoặc can thiệp sớm.")

safety_t, safety_n = resolve_table(creds, "fact_safety_incident")
trip_t, _ = resolve_table(creds, "fact_trip", required=False)
date_t, _ = resolve_table(creds, "dim_date")
driver_t, driver_n = resolve_table(creds, "dim_driver", required=False)

schema = table_schema(creds, safety_n)
cols = list(schema)
NONE = "(không có)"

# ---------------------------------------------------------------------------
# Dò cột, cho phép đổi
# ---------------------------------------------------------------------------
COST_KEYS = ("damage", "claim", "cost", "amount", "payout", "repair", "medical", "settlement")
COST_SKIP = ("count", "number", "_id", "flag", "_key")
numeric_cols = [c for c, t in schema.items() if is_numeric_type(t) and not c.endswith("_key")]
auto_cost = [c for c in numeric_cols if any(k in c.lower() for k in COST_KEYS) and not any(s in c.lower() for s in COST_SKIP)]
auto_date = pick(cols, "date_key", "incident_date_key", "event_date_key") or next((c for c in cols if c.endswith("date_key")), None)

with st.sidebar.expander("Cột dữ liệu (tự dò, có thể đổi)"):
    driver_col = st.selectbox("Khóa tài xế", cols, index=cols.index(pick(cols, "driver_key")) if pick(cols, "driver_key") else 0)
    date_col = st.selectbox("Khóa ngày", cols, index=cols.index(auto_date) if auto_date else 0)
    fault_auto = pick(cols, "is_at_fault", "at_fault_flag", "at_fault", "is_at_fault_flag", "at_fault_indicator")
    prev_auto = pick(cols, "is_preventable", "preventable_flag", "preventable", "is_preventable_flag")
    fault_col = st.selectbox("Cột lỗi của tài xế (at-fault)", [NONE] + cols, index=([NONE] + cols).index(fault_auto) if fault_auto else 0)
    prev_col = st.selectbox("Cột có thể phòng tránh (preventable)", [NONE] + cols, index=([NONE] + cols).index(prev_auto) if prev_auto else 0)
    cost_cols = st.multiselect("Cột chi phí (cộng lại)", numeric_cols, default=auto_cost)
    st.caption("Cột kiểu 0/1, true/false hoặc Y/N đều dùng được cho at-fault và preventable.")

if pick(cols, "driver_key") is None and driver_col is None:
    st.error("Bảng sự cố không có khóa tài xế.")
    st.stop()


def flag(col: str) -> str:
    return f"toUInt8(lower(toString(s.{qi(col)})) IN ('1', 'true', 'y', 'yes', 't'))"


fault_expr = flag(fault_col) if fault_col != NONE else "0"
prev_expr = flag(prev_col) if prev_col != NONE else "0"
cost_expr = " + ".join(f"ifNull(toFloat64OrZero(toString(s.{qi(c)})), 0)" for c in cost_cols) or "0"

# ---------------------------------------------------------------------------
# Truy vấn: một dòng một tài xế một tháng
# ---------------------------------------------------------------------------
SQL = f"""
SELECT s.{qi(driver_col)} AS dk,
       toStartOfMonth(dd.full_date) AS mth,
       count() AS incidents,
       sum({fault_expr}) AS at_fault,
       sum({prev_expr}) AS preventable,
       sum({cost_expr}) AS cost
FROM {safety_t} AS s
INNER JOIN {date_t} AS dd ON s.{qi(date_col)} = dd.date_key
GROUP BY dk, mth
"""
try:
    raw = run_query_slow(SQL, creds)
except Exception as e:
    st.error("Truy vấn sự cố bị lỗi. Kiểm tra lại các cột ở mục 'Cột dữ liệu' ở thanh bên.")
    st.code(str(e))
    with st.expander("Các cột của bảng sự cố"):
        st.dataframe(pd.DataFrame({"cột": list(schema), "kiểu": list(schema.values())}), hide_index=True)
    st.stop()

raw = ensure_cols(raw, ["dk", "mth", "incidents", "at_fault", "preventable", "cost"])
if raw.empty:
    st.warning("Chưa có dữ liệu sự cố.")
    st.stop()
raw["mth"] = pd.to_datetime(raw["mth"])

# Số dặm theo tài xế và tháng, để chuẩn hóa sự cố trên 100.000 dặm
miles = pd.DataFrame(columns=["dk", "mth", "miles"])
if trip_t:
    try:
        miles = run_query_slow(
            f"""SELECT t.driver_key AS dk, toStartOfMonth(dd.full_date) AS mth, toFloat64(sum(t.actual_miles)) AS miles
                FROM {trip_t} AS t INNER JOIN {date_t} AS dd ON t.date_key = dd.date_key GROUP BY dk, mth""",
            creds,
        )
        miles = ensure_cols(miles, ["dk", "mth", "miles"])
        miles["mth"] = pd.to_datetime(miles["mth"])
    except Exception:
        miles = pd.DataFrame(columns=["dk", "mth", "miles"])

# Nhãn tài xế
labels = pd.DataFrame(columns=["dk", "driver"])
if driver_t:
    dc = table_columns(creds, driver_n)
    name_col = pick(dc, "driver_name", "full_name", "name")
    if name_col:
        expr = f"toString({qi(name_col)})"
    elif pick(dc, "first_name") and pick(dc, "last_name"):
        expr = f"concat(toString({qi(pick(dc, 'first_name'))}), ' ', toString({qi(pick(dc, 'last_name'))}))"
    else:
        expr = "toString(driver_key)"
    try:
        labels = ensure_cols(
            run_query_slow(f"SELECT driver_key AS dk, any({expr}) AS driver FROM {driver_t} GROUP BY dk", creds),
            ["dk", "driver"],
        )
    except Exception:
        pass


def label_of(dk):
    m = labels.loc[labels["dk"] == dk, "driver"]
    return str(m.iloc[0]) if len(m) else str(dk)


# ---------------------------------------------------------------------------
# Tham số
# ---------------------------------------------------------------------------
months = sorted(raw["mth"].unique(), reverse=True)
st.sidebar.divider()
st.sidebar.subheader("Tham số")
max_date = None
if trip_t:
    try:
        max_date = pd.to_datetime(run_query_slow(
            f"SELECT max(dd.full_date) AS d FROM {trip_t} AS t INNER JOIN {date_t} AS dd ON t.date_key = dd.date_key", creds)["d"].iloc[0])
    except Exception:
        max_date = None
# mặc định chọn tháng đã đủ ngày gần nhất
default_i = 1 if len(months) > 1 and month_is_partial(months[0], max_date) else 0
sel = pd.Timestamp(st.sidebar.selectbox(
    "Tháng", months, index=default_i,
    format_func=lambda m: pd.Timestamp(m).strftime("%m/%Y") + (" (chưa đủ tháng)" if month_is_partial(m, max_date) else ""),
))
if month_is_partial(sel, max_date):
    st.warning(f"Dữ liệu mới nhất đến {max_date:%d/%m/%Y}, nên tháng {sel:%m/%Y} chưa đủ ngày. Số sự cố của tháng này có thể thấp hơn thực tế.")
window = 1 if st.sidebar.radio(
    "Cửa sổ tính điểm", ["1 tháng", "3 tháng gần nhất"],
    help="Sự cố ít xảy ra nên điểm theo 1 tháng dao động mạnh. Cửa sổ 3 tháng ổn định hơn.",
).startswith("1") else 3
pct = st.sidebar.slider("Gắn cờ nhóm rủi ro cao nhất (%)", 5, 30, 10)
min_inc = st.sidebar.number_input("Số sự cố tối thiểu để được xếp hạng", 1, 10, 1)

METRICS = ["incidents", "at_fault_rate", "preventable_rate", "cost"]


def window_stats(month: pd.Timestamp) -> pd.DataFrame:
    start = month - pd.DateOffset(months=window - 1)
    sub = raw[(raw["mth"] >= start) & (raw["mth"] <= month)]
    g = sub.groupby("dk", as_index=False)[["incidents", "at_fault", "preventable", "cost"]].sum()
    g["at_fault_rate"] = g["at_fault"] / g["incidents"] * 100
    g["preventable_rate"] = g["preventable"] / g["incidents"] * 100
    if len(miles):
        mi = miles[(miles["mth"] >= start) & (miles["mth"] <= month)].groupby("dk", as_index=False)["miles"].sum()
        g = g.merge(mi, on="dk", how="left")
        g["per_100k_miles"] = np.where(g["miles"] > 0, g["incidents"] / g["miles"] * 100000, np.nan)
    else:
        g["miles"], g["per_100k_miles"] = np.nan, np.nan
    return g


def score(g: pd.DataFrame) -> pd.DataFrame:
    g = g[g["incidents"] >= min_inc].copy()
    if g.empty:
        g["score"], g["rank"], g["flagged"] = [], [], []
        return g
    ranks = pd.concat([g[c].rank(pct=True) for c in METRICS], axis=1)
    g["score"] = ranks.mean(axis=1, skipna=True)
    g = g.sort_values(["score", "cost", "incidents"], ascending=False).reset_index(drop=True)
    g["rank"] = np.arange(1, len(g) + 1)
    k = max(1, math.ceil(len(g) * pct / 100))
    g["flagged"] = g["rank"] <= k
    return g


def signals(r) -> str:
    out = []
    if r["incidents"] >= 2:
        out.append(f"{int(r['incidents'])} sự cố")
    if pd.notna(r["at_fault_rate"]) and r["at_fault_rate"] >= 50:
        out.append("thường là lỗi của tài xế")
    if pd.notna(r["preventable_rate"]) and r["preventable_rate"] >= 50:
        out.append("nhiều sự cố phòng tránh được")
    if r["cost"] > 0:
        out.append(f"thiệt hại ${r['cost']:,.0f}")
    return ", ".join(out)


stats = window_stats(sel)
scored = score(stats)
scored["driver"] = scored["dk"].map(label_of) if len(scored) else []
scored["signals"] = scored.apply(signals, axis=1) if len(scored) else []

# ---------------------------------------------------------------------------
# Chỉ số tổng quan
# ---------------------------------------------------------------------------
period = sel.strftime("%m/%Y") if window == 1 else f"{(sel - pd.DateOffset(months=2)).strftime('%m/%Y')} - {sel.strftime('%m/%Y')}"
tot_inc = int(stats["incidents"].sum()) if len(stats) else 0
k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("Kỳ", period)
k2.metric("Sự cố", tot_inc)
k3.metric("Tài xế có sự cố", len(stats))
k4.metric("Tỷ lệ lỗi tài xế", f"{stats['at_fault'].sum() / tot_inc * 100:.0f}%" if tot_inc and fault_col != NONE else "n/a")
k5.metric("Tỷ lệ phòng tránh được", f"{stats['preventable'].sum() / tot_inc * 100:.0f}%" if tot_inc and prev_col != NONE else "n/a")
k6.metric("Chi phí thiệt hại", f"${stats['cost'].sum():,.0f}" if len(stats) else "$0")

if fault_col == NONE or prev_col == NONE:
    st.warning("Chưa dò được cột at-fault hoặc preventable. Hãy chọn ở mục 'Cột dữ liệu' ở thanh bên để có tỷ lệ tương ứng.")
if not cost_cols:
    st.warning("Chưa chọn cột chi phí nào, nên tổng thiệt hại bằng 0.")

tab_risk, tab_all, tab_trend, tab_driver = st.tabs(
    ["Tài xế rủi ro cao", "Toàn bộ tài xế có sự cố", "Xu hướng", "Chi tiết từng tài xế"]
)

SHOW = {
    "rank": "Hạng", "driver": "Tài xế", "incidents": "Sự cố", "at_fault_rate": "Lỗi tài xế (%)",
    "preventable_rate": "Phòng tránh được (%)", "cost": "Thiệt hại ($)", "per_100k_miles": "Sự cố/100k dặm",
    "score": "Điểm", "signals": "Tín hiệu",
}
CFG = {
    "Lỗi tài xế (%)": st.column_config.NumberColumn(format="%.0f"),
    "Phòng tránh được (%)": st.column_config.NumberColumn(format="%.0f"),
    "Thiệt hại ($)": st.column_config.NumberColumn(format="$%,.0f"),
    "Sự cố/100k dặm": st.column_config.NumberColumn(format="%.2f"),
    "Điểm": st.column_config.NumberColumn(format="%.2f"),
}

with tab_risk:
    st.caption(
        f"Điểm là trung bình thứ hạng phần trăm của số sự cố, tỷ lệ lỗi tài xế, tỷ lệ phòng tránh được và chi phí thiệt hại "
        f"trong nhóm tài xế có sự cố. Nhóm cao nhất ({pct}%) bị gắn cờ."
    )
    if scored.empty:
        st.info("Không có tài xế nào có sự cố trong kỳ này.")
    else:
        flagged = scored[scored["flagged"]]
        view = flagged[list(SHOW)].rename(columns=SHOW)
        st.dataframe(view, width="stretch", hide_index=True, column_config=CFG)
        st.download_button("Tải CSV", view.to_csv(index=False).encode("utf-8"),
                           file_name=f"high_risk_drivers_{sel:%Y_%m}.csv", mime="text/csv")

        st.subheader("Tài xế bị gắn cờ lặp lại trong 6 tháng gần nhất")
        recent = [m for m in sorted(raw["mth"].unique()) if m <= sel][-6:]
        flagged_counts = {}
        for m in recent:
            sc = score(window_stats(pd.Timestamp(m)))
            for dk in sc.loc[sc["flagged"], "dk"]:
                flagged_counts[dk] = flagged_counts.get(dk, 0) + 1
        rep = pd.DataFrame(
            [{"Tài xế": label_of(dk), "Số tháng bị gắn cờ": n} for dk, n in flagged_counts.items() if n >= 2]
        )
        if rep.empty:
            st.info("Chưa có tài xế nào bị gắn cờ từ 2 tháng trở lên.")
        else:
            st.dataframe(rep.sort_values("Số tháng bị gắn cờ", ascending=False), width="stretch", hide_index=True)

with tab_all:
    if scored.empty:
        st.info("Không có dữ liệu.")
    else:
        st.dataframe(scored[list(SHOW)].rename(columns=SHOW), width="stretch", hide_index=True, column_config=CFG)

with tab_trend:
    st.caption("Mục tiêu của BR-07 là giảm số sự cố phòng tránh được sau 3 tháng áp dụng, nên theo dõi xu hướng này theo tháng.")
    monthly = raw.groupby("mth")[["incidents", "at_fault", "preventable", "cost"]].sum()
    c1, c2 = st.columns(2)
    c1.write("Số sự cố theo tháng")
    c1.line_chart(monthly[["incidents", "at_fault", "preventable"]].rename(
        columns={"incidents": "Tổng", "at_fault": "Lỗi tài xế", "preventable": "Phòng tránh được"}))
    c2.write("Chi phí thiệt hại theo tháng ($)")
    c2.line_chart(monthly["cost"])
    if prev_col != NONE:
        st.write("Tỷ lệ sự cố phòng tránh được (%)")
        st.line_chart((monthly["preventable"] / monthly["incidents"].where(monthly["incidents"] > 0) * 100).rename("Tỷ lệ"))

with tab_driver:
    candidates = scored["dk"].tolist() if len(scored) else raw["dk"].drop_duplicates().tolist()
    if not candidates:
        st.info("Không có tài xế.")
    else:
        pick_dk = st.selectbox("Tài xế", candidates, format_func=label_of)
        one = raw[raw["dk"] == pick_dk].set_index("mth").sort_index()
        a, b = st.columns(2)
        a.write("Số sự cố theo tháng")
        a.bar_chart(one["incidents"])
        b.write("Chi phí thiệt hại theo tháng ($)")
        b.bar_chart(one["cost"])
        st.write("Các sự cố gần đây")
        try:
            recent_inc = run_query(
                f"""SELECT dd.full_date AS incident_date, s.*
                    FROM {safety_t} AS s INNER JOIN {date_t} AS dd ON s.{qi(date_col)} = dd.date_key
                    WHERE s.{qi(driver_col)} = {int(pick_dk)} ORDER BY incident_date DESC LIMIT 100""",
                creds,
            )
            st.dataframe(recent_inc, width="stretch", hide_index=True)
        except Exception as e:
            st.warning("Không lấy được danh sách sự cố.")
            st.caption(str(e)[:300])
