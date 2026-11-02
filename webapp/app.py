"""Logistics Operations: điểm vào của ứng dụng. Mỗi nhóm người dùng có một trang riêng trong thư mục roles/:

  Quản lý            : tổng quan, nhu cầu theo mùa (BR-06), mức đáp ứng các yêu cầu nghiệp vụ
  Điều phối          : cảnh báo trễ hạn (BR-02), tra cứu lô hàng (BR-03)
  Đội xe và An toàn  : hiệu suất xe (BR-01), an toàn tài xế (BR-07)
  Tài chính và Kế hoạch: lợi nhuận theo tuyến (BR-05), nhu cầu theo mùa (BR-06)
  Kinh doanh         : doanh thu khách hàng (BR-08)
  Nền tảng dữ liệu   : chất lượng dữ liệu (BR-04), mô hình lô hàng (BR-03), khám phá dữ liệu

Logic từng chức năng nằm trong views/. File này chỉ lo điều hướng."""

import streamlit as st
from clickhouse_connect.driver.exceptions import OperationalError

from common import show_connection_error

st.set_page_config(page_title="Logistics Operations", page_icon=":material/local_shipping:", layout="wide")

PAGES = [
    st.Page("roles/manager.py", title="Quản lý", icon=":material/dashboard:", url_path="quan-ly", default=True),
    st.Page("roles/dispatch.py", title="Điều phối", icon=":material/support_agent:", url_path="dieu-phoi"),
    st.Page("roles/fleet_safety.py", title="Đội xe và An toàn", icon=":material/local_shipping:", url_path="doi-xe-an-toan"),
    st.Page("roles/finance.py", title="Tài chính và Kế hoạch", icon=":material/payments:", url_path="tai-chinh"),
    st.Page("roles/sales.py", title="Kinh doanh", icon=":material/groups:", url_path="kinh-doanh"),
    st.Page("roles/data_platform.py", title="Nền tảng dữ liệu", icon=":material/database:", url_path="du-lieu"),
]

navigation = st.navigation(PAGES)
try:
    navigation.run()
except OperationalError as e:
    show_connection_error(e)
