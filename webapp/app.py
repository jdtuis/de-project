"""Logistics Operations: điểm vào của ứng dụng.

File này chỉ lo điều hướng. Mỗi trang nằm trong thư mục pages/ và được nhóm theo người dùng:
  Tổng quan            : chỉ số chính + ma trận yêu cầu nghiệp vụ (BR)
  Vận hành lô hàng     : cảnh báo trễ hạn (BR-02), tra cứu lô hàng (BR-03)
  Đội xe và An toàn    : hiệu suất xe (BR-01), an toàn tài xế (BR-07)
  Doanh thu và Khách hàng: lợi nhuận tuyến (BR-05), khách hàng (BR-08), mùa vụ (BR-06)
  Dữ liệu              : chất lượng dữ liệu (BR-04), khám phá dữ liệu
Trang nào thiếu file trong pages/ sẽ tự bị bỏ khỏi menu.
"""

import os

import streamlit as st

st.set_page_config(page_title="Logistics Operations", page_icon=":material/local_shipping:", layout="wide")

HERE = os.path.dirname(os.path.abspath(__file__))


def page(path: str, title: str, icon: str, default: bool = False):
    if not os.path.exists(os.path.join(HERE, path)):
        return None
    return st.Page(path, title=title, icon=icon, default=default)


GROUPS = {
    "Tổng quan": [
        page("pages/0_Overview.py", "Tổng quan", ":material/home:", default=True),
        page("pages/9_BR_Coverage.py", "Yêu cầu nghiệp vụ", ":material/fact_check:"),
    ],
    "Vận hành lô hàng": [
        page("pages/2_Shipment_Alerts.py", "Cảnh báo trễ hạn", ":material/notifications_active:"),
        page("pages/3_Unified_Shipment_View.py", "Tra cứu lô hàng", ":material/search:"),
    ],
    "Đội xe và An toàn": [
        page("pages/1_Fleet_Performance.py", "Hiệu suất đội xe", ":material/local_shipping:"),
        page("pages/7_Driver_Safety.py", "An toàn tài xế", ":material/health_and_safety:"),
    ],
    "Doanh thu và Khách hàng": [
        page("pages/5_Route_Profitability.py", "Lợi nhuận theo tuyến", ":material/alt_route:"),
        page("pages/8_Customer_Revenue.py", "Doanh thu khách hàng", ":material/groups:"),
        page("pages/6_Seasonal_Demand.py", "Nhu cầu theo mùa", ":material/calendar_month:"),
    ],
    "Dữ liệu": [
        page("pages/4_Data_Quality.py", "Chất lượng dữ liệu", ":material/rule:"),
        page("pages/10_Data_Explorer.py", "Khám phá dữ liệu", ":material/table_view:"),
    ],
}
GROUPS = {name: [p for p in pages if p is not None] for name, pages in GROUPS.items()}
GROUPS = {name: pages for name, pages in GROUPS.items() if pages}

navigation = st.navigation(GROUPS)
navigation.run()
