"""Trang Kinh doanh: tập trung doanh thu và khách hàng có dấu hiệu suy giảm."""

import rolepage
from views import customers

rolepage.run(
    title="Kinh doanh",
    audience="Sales và Account Management",
    brs="BR-08",
    views={"Doanh thu khách hàng": customers.render},
    key="sales",
)
