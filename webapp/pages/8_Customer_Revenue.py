"""BR-08: Customer Revenue Concentration & Retention.

Doanh thu theo khách hàng theo tháng (v_fact_trip nối dim_customer), gồm số lô, giá cước trung bình và tỷ trọng.
  - Tập trung: tỷ trọng của khách hàng lớn nhất, top 5, nhóm 20% khách hàng đầu và chỉ số HHI, mỗi tháng
  - Suy giảm : so doanh thu N tháng gần nhất với N tháng liền trước, gắn cờ khách hàng giảm mạnh hoặc ngừng phát sinh
"""

import math

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from common import ensure_cols, get_creds, month_is_partial, pick, qi, resolve_table, run_query_slow, table_columns

st.set_page_config(page_title="Customer revenue", layout="wide")
creds = get_creds()

st.title("Customer revenue and retention")
st.caption("Mức độ phụ thuộc vào các khách hàng lớn và những tài khoản có dấu hiệu suy giảm, để Sales và Finance can thiệp sớm.")

trip_t, _ = resolve_table(creds, "fact_trip")
date_t, _ = resolve_table(creds, "dim_date")
cust_t, cust_n = resolve_table(creds, "dim_customer")

# ---------------------------------------------------------------------------
# Dữ liệu
# ---------------------------------------------------------------------------
SQL_MONTHLY = f"""
SELECT t.customer_key AS ck,
       toStartOfMonth(dd.full_date) AS mth,
       uniqExact(t.load_id) AS loads,
       toFloat64(sum(t.total_revenue)) AS revenue,
       toFloat64(sum(t.actual_miles)) AS miles
FROM {trip_t} AS t
INNER JOIN {date_t} AS dd ON t.date_key = dd.date_key
GROUP BY ck, mth
"""

cc = table_columns(creds, cust_n)
name_col = pick(cc, "customer_name", "name", "company_name")
PROFILE = [
    ("customer_type", "Loại", False),
    ("account_status", "Trạng thái", False),
    ("primary_freight_type", "Loại hàng chính", False),
    ("credit_terms_days", "Hạn thanh toán (ngày)", True),
    ("contract_start_date", "Bắt đầu hợp đồng", False),
    ("annual_revenue_potential", "Tiềm năng doanh thu năm ($)", True),
]
prof = [(pick(cc, col), label, numeric) for col, label, numeric in PROFILE if pick(cc, col)]
sel_exprs = [f"any(toString({qi(name_col)})) AS customer" if name_col else "any(toString(customer_key)) AS customer"]
for i, (col, _, numeric) in enumerate(prof):
    sel_exprs.append(
        f"any(toFloat64OrNull(toString({qi(col)}))) AS p{i}" if numeric else f"any(toString({qi(col)})) AS p{i}"
    )
SQL_PROFILE = f"SELECT customer_key AS ck, {', '.join(sel_exprs)} FROM {cust_t} GROUP BY ck"

try:
    raw = run_query_slow(SQL_MONTHLY, creds)
    profile = run_query_slow(SQL_PROFILE, creds)
except Exception as e:
    st.error("Truy vấn bị lỗi.")
    st.code(str(e))
    st.stop()

raw = ensure_cols(raw, ["ck", "mth", "loads", "revenue", "miles"])
profile = ensure_cols(profile, ["ck", "customer"] + [f"p{i}" for i in range(len(prof))])
if raw.empty:
    st.warning("Chưa có dữ liệu chuyến đi.")
    st.stop()
raw["mth"] = pd.to_datetime(raw["mth"])

all_months = pd.date_range(raw["mth"].min(), raw["mth"].max(), freq="MS")
rev = raw.pivot_table(index="ck", columns="mth", values="revenue", aggfunc="sum").reindex(columns=all_months).fillna(0.0)
loads = raw.pivot_table(index="ck", columns="mth", values="loads", aggfunc="sum").reindex(columns=all_months).fillna(0)
miles = raw.pivot_table(index="ck", columns="mth", values="miles", aggfunc="sum").reindex(columns=all_months).fillna(0.0)

names = dict(zip(profile["ck"], profile["customer"]))
unassigned_keys = [k for k in rev.index if k not in names or k <= 0]


def cname(ck) -> str:
    if ck in names and ck > 0:
        return names[ck]
    return "Chưa gán khách hàng" if ck <= 0 else f"Khách hàng #{ck}"


# ---------------------------------------------------------------------------
# Tham số
# ---------------------------------------------------------------------------
st.sidebar.divider()
st.sidebar.subheader("Tham số")
try:
    max_date = pd.to_datetime(run_query_slow(
        f"SELECT max(dd.full_date) AS d FROM {trip_t} AS t INNER JOIN {date_t} AS dd ON t.date_key = dd.date_key", creds)["d"].iloc[0])
except Exception:
    max_date = None
month_opts = list(reversed(all_months))
# mặc định chọn tháng đã đủ ngày gần nhất
default_i = 1 if len(month_opts) > 1 and month_is_partial(month_opts[0], max_date) else 0
M = pd.Timestamp(st.sidebar.selectbox(
    "Tháng", month_opts, index=default_i,
    format_func=lambda m: pd.Timestamp(m).strftime("%m/%Y") + (" (chưa đủ tháng)" if month_is_partial(m, max_date) else ""),
))
if month_is_partial(M, max_date):
    st.warning(f"Dữ liệu mới nhất đến {max_date:%d/%m/%Y}, nên tháng {M:%m/%Y} chưa đủ ngày. Doanh thu và cảnh báo suy giảm của tháng này có thể bị thấp hơn thực tế.")
k = int(st.sidebar.radio("Cửa sổ so sánh (tháng)", [3, 6], horizontal=True,
                         help="So doanh thu k tháng gần nhất với k tháng liền trước đó."))
decline_thr = st.sidebar.slider("Ngưỡng suy giảm (%)", 5, 80, 20)
conc_thr = st.sidebar.slider("Cảnh báo khi một khách hàng chiếm trên (%)", 5, 50, 10)

pos = list(all_months).index(M)
cur_months = all_months[max(0, pos - k + 1): pos + 1]
prev_months = all_months[max(0, pos - 2 * k + 1): max(0, pos - k + 1)]
can_compare = pos - 2 * k + 1 >= 0


# ---------------------------------------------------------------------------
# Tập trung doanh thu
# ---------------------------------------------------------------------------
def top_share(series: pd.Series, frac: float = 0.2) -> float:
    s = series[series > 0].sort_values(ascending=False)
    if s.empty:
        return np.nan
    n_top = max(1, math.ceil(frac * len(s)))
    return s.head(n_top).sum() / s.sum()


def hhi(series: pd.Series) -> float:
    s = series[series > 0]
    return float(((s / s.sum()) ** 2).sum() * 10000) if len(s) else np.nan


cur_rev = rev[M]
active = cur_rev[cur_rev > 0].sort_values(ascending=False)
total = float(active.sum())
share = active / total if total else active

k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("Khách hàng hoạt động", len(active))
k2.metric("Doanh thu tháng", f"${total:,.0f}")
k3.metric("Khách hàng lớn nhất", f"{share.iloc[0] * 100:.1f}%" if len(share) else "n/a")
k4.metric("Top 5", f"{share.head(5).sum() * 100:.1f}%" if len(share) else "n/a")
k5.metric("20% khách hàng đầu", f"{top_share(cur_rev) * 100:.1f}%" if len(share) else "n/a")
k6.metric("Chỉ số HHI", f"{hhi(cur_rev):,.0f}" if len(share) else "n/a",
          help="Tổng bình phương tỷ trọng x 10.000. Dưới 1.500 là phân tán, trên 2.500 là tập trung cao.")

# Tính toán bảng khách hàng
cur = rev[cur_months].sum(axis=1)
prev = rev[prev_months].sum(axis=1) if can_compare else pd.Series(0.0, index=rev.index)
mom = np.where(rev[all_months[pos - 1]] > 0, (rev[M] / rev[all_months[pos - 1]].where(rev[all_months[pos - 1]] > 0) - 1) * 100, np.nan) if pos > 0 else np.full(len(rev), np.nan)

tbl = pd.DataFrame({"ck": rev.index})
tbl["customer"] = tbl["ck"].map(cname)
tbl["revenue_m"] = rev[M].values
tbl["share"] = (tbl["revenue_m"] / total * 100) if total else np.nan
tbl["loads_m"] = loads[M].values
tbl["rate"] = np.where(miles[M].values > 0, rev[M].values / np.where(miles[M].values > 0, miles[M].values, 1), np.nan)
tbl["mom"] = mom
tbl["cur"] = cur.values
tbl["prev"] = prev.values
tbl["change"] = np.where(tbl["prev"] > 0, (tbl["cur"] - tbl["prev"]) / tbl["prev"].where(tbl["prev"] > 0) * 100, np.nan)
last12 = list(all_months[max(0, pos - 11): pos + 1])
tbl["trend"] = [list(map(float, rev.loc[ck, last12].values)) for ck in tbl["ck"]]

if can_compare:
    tbl["decline"] = (tbl["prev"] > 0) & (tbl["cur"] > 0) & (tbl["change"] <= -decline_thr)
    tbl["lost"] = (tbl["prev"] > 0) & (tbl["cur"] == 0)
    tbl["new"] = (tbl["prev"] == 0) & (tbl["cur"] > 0)
else:
    tbl["decline"] = tbl["lost"] = tbl["new"] = False
tbl["concentration"] = tbl["share"].fillna(0) >= conc_thr


def alerts(r) -> str:
    out = []
    if r["concentration"]:
        out.append("tập trung cao")
    if r["decline"]:
        out.append(f"giảm {abs(r['change']):.0f}%")
    if r["lost"]:
        out.append("ngừng phát sinh")
    if r["new"]:
        out.append("mới")
    return ", ".join(out)


tbl["alerts"] = tbl.apply(alerts, axis=1)

# gắn thông tin hồ sơ khách hàng
prof_idx = profile.set_index("ck")
for i, (_, label, _) in enumerate(prof):
    tbl[label] = tbl["ck"].map(prof_idx[f"p{i}"])

tab_conc, tab_cust, tab_alert = st.tabs(["Tập trung doanh thu", "Khách hàng", "Cảnh báo suy giảm"])

# ---------------------------------------------------------------------------
with tab_conc:
    if share.empty:
        st.info("Tháng này không có doanh thu.")
    else:
        a, b = st.columns(2)
        curve = pd.DataFrame({
            "Khách hàng (% tích lũy)": np.arange(1, len(share) + 1) / len(share) * 100,
            "Doanh thu (% tích lũy)": share.cumsum().values * 100,
        })
        line = alt.Chart(curve).mark_line().encode(
            x=alt.X("Khách hàng (% tích lũy):Q", scale=alt.Scale(domain=[0, 100])),
            y=alt.Y("Doanh thu (% tích lũy):Q", scale=alt.Scale(domain=[0, 100])),
        )
        diag = alt.Chart(pd.DataFrame({"x": [0, 100], "y": [0, 100]})).mark_line(strokeDash=[4, 4], color="gray").encode(x="x:Q", y="y:Q")
        rule = alt.Chart(pd.DataFrame({"x": [20]})).mark_rule(color="red", strokeDash=[2, 2]).encode(x="x:Q")
        a.write("Đường tập trung doanh thu của tháng đã chọn")
        a.altair_chart(line + diag + rule, width="stretch")
        a.caption("Đường càng cong lên cao phía trái thì doanh thu càng tập trung vào ít khách hàng. Vạch đỏ là mốc 20% khách hàng.")

        by_month = pd.DataFrame({
            "20% khách hàng đầu": [top_share(rev[m]) * 100 for m in all_months],
            "Khách hàng lớn nhất": [(rev[m].max() / rev[m].sum() * 100) if rev[m].sum() > 0 else np.nan for m in all_months],
        }, index=all_months)
        b.write("Tỷ trọng doanh thu theo tháng (%)")
        b.line_chart(by_month)

        n_unassigned = float(rev.loc[unassigned_keys, M].sum()) if unassigned_keys else 0.0
        st.caption(
            f"Doanh thu của tháng chưa gán được cho khách hàng nào: ${n_unassigned:,.0f} "
            f"({n_unassigned / total * 100:.1f}%)." if total else ""
        )
        over = tbl[tbl["concentration"]].sort_values("revenue_m", ascending=False)
        if len(over):
            st.warning(f"{len(over)} khách hàng chiếm từ {conc_thr}% doanh thu tháng: " + ", ".join(over["customer"].head(5)))

# ---------------------------------------------------------------------------
with tab_cust:
    f1, f2, f3 = st.columns([2, 1, 1])
    query = f1.text_input("Tìm khách hàng")
    only_alert = f2.checkbox("Chỉ hiện khách hàng có cảnh báo")
    only_active = f3.checkbox("Chỉ khách hàng có doanh thu tháng này", value=True)
    view = tbl[(tbl["cur"] + tbl["prev"] + tbl["revenue_m"]) > 0]
    if only_active:
        view = view[view["revenue_m"] > 0]
    if only_alert:
        view = view[view["alerts"] != ""]
    if query.strip():
        view = view[view["customer"].str.contains(query.strip(), case=False, na=False)]
    view = view.sort_values("revenue_m", ascending=False).head(500)

    cols = {
        "customer": "Khách hàng", "revenue_m": "Doanh thu tháng ($)", "share": "Tỷ trọng (%)", "loads_m": "Số lô",
        "rate": "Giá cước ($/dặm)", "mom": "So tháng trước (%)", "change": f"So {k} tháng trước (%)",
        "trend": "Xu hướng 12 tháng", "alerts": "Cảnh báo",
    }
    out = view[list(cols)].rename(columns=cols)
    for _, label, _ in prof:
        out[label] = view[label].values
    st.dataframe(
        out, width="stretch", hide_index=True,
        column_config={
            "Doanh thu tháng ($)": st.column_config.NumberColumn(format="$%,.0f"),
            "Tỷ trọng (%)": st.column_config.NumberColumn(format="%.1f"),
            "Giá cước ($/dặm)": st.column_config.NumberColumn(format="$%.2f"),
            "So tháng trước (%)": st.column_config.NumberColumn(format="%.0f"),
            f"So {k} tháng trước (%)": st.column_config.NumberColumn(format="%.0f"),
            "Xu hướng 12 tháng": st.column_config.LineChartColumn("Xu hướng 12 tháng", y_min=0),
            "Tiềm năng doanh thu năm ($)": st.column_config.NumberColumn(format="$%,.0f"),
        },
    )
    csv = out.drop(columns=["Xu hướng 12 tháng"]).to_csv(index=False).encode("utf-8")
    st.download_button("Tải CSV", csv, file_name=f"customer_revenue_{M:%Y_%m}.csv", mime="text/csv")

    st.write("Doanh thu hàng tháng của một khách hàng")
    options = view["ck"].tolist()
    if options:
        ck = st.selectbox("Khách hàng", options, format_func=cname)
        one = pd.DataFrame({"Doanh thu": rev.loc[ck], "Số lô": loads.loc[ck]})
        c1, c2 = st.columns(2)
        c1.bar_chart(one["Doanh thu"])
        c2.bar_chart(one["Số lô"])

# ---------------------------------------------------------------------------
with tab_alert:
    if not can_compare:
        st.info(
            f"Chưa đủ lịch sử để so sánh {k} tháng với {k} tháng trước đó tại tháng này. "
            f"Chọn tháng muộn hơn hoặc giảm cửa sổ so sánh."
        )
    else:
        st.caption(
            f"So doanh thu {cur_months[0]:%m/%Y} - {cur_months[-1]:%m/%Y} với {prev_months[0]:%m/%Y} - {prev_months[-1]:%m/%Y}. "
            f"Giảm từ {decline_thr}% trở lên là suy giảm. Có doanh thu kỳ trước nhưng không có kỳ này là ngừng phát sinh."
        )
        flagged = tbl[tbl["decline"] | tbl["lost"]].copy()
        flagged["at_risk"] = (flagged["prev"] - flagged["cur"]) / k
        a, b, c, d = st.columns(4)
        a.metric("Khách hàng suy giảm", int(tbl["decline"].sum()))
        b.metric("Ngừng phát sinh", int(tbl["lost"].sum()))
        c.metric("Doanh thu/tháng đang có nguy cơ mất", f"${flagged['at_risk'].sum():,.0f}")
        d.metric("So với doanh thu tháng", f"{flagged['at_risk'].sum() / total * 100:.1f}%" if total else "n/a")
        if flagged.empty:
            st.success("Không có khách hàng nào suy giảm hoặc ngừng phát sinh theo ngưỡng này.")
        else:
            show = flagged.sort_values("at_risk", ascending=False)
            out = pd.DataFrame({
                "Khách hàng": show["customer"],
                "Tình trạng": np.where(show["lost"], "Ngừng phát sinh", "Suy giảm"),
                f"TB/tháng kỳ trước ($)": show["prev"] / k,
                f"TB/tháng kỳ này ($)": show["cur"] / k,
                "Thay đổi (%)": show["change"],
                "Nguy cơ mất/tháng ($)": show["at_risk"],
                "Xu hướng 12 tháng": show["trend"],
            })
            for _, label, _ in prof:
                out[label] = show[label].values
            st.dataframe(
                out, width="stretch", hide_index=True,
                column_config={
                    "TB/tháng kỳ trước ($)": st.column_config.NumberColumn(format="$%,.0f"),
                    "TB/tháng kỳ này ($)": st.column_config.NumberColumn(format="$%,.0f"),
                    "Thay đổi (%)": st.column_config.NumberColumn(format="%.0f"),
                    "Nguy cơ mất/tháng ($)": st.column_config.NumberColumn(format="$%,.0f"),
                    "Xu hướng 12 tháng": st.column_config.LineChartColumn("Xu hướng 12 tháng", y_min=0),
                    "Tiềm năng doanh thu năm ($)": st.column_config.NumberColumn(format="$%,.0f"),
                },
            )
            st.download_button(
                "Tải CSV", out.drop(columns=["Xu hướng 12 tháng"]).to_csv(index=False).encode("utf-8"),
                file_name=f"declining_customers_{M:%Y_%m}.csv", mime="text/csv",
            )
