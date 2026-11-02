"""Trang Nền tảng dữ liệu: kiểm tra chất lượng, mô hình lô hàng và khám phá dữ liệu."""

import rolepage
from views import data_quality, explorer, shipments

rolepage.run(
    title="Nền tảng dữ liệu",
    audience="Data Platform Team",
    brs="BR-03, BR-04",
    views={
        "Chất lượng dữ liệu": data_quality.render,
        "Mô hình lô hàng": shipments.render_model,
        "Khám phá dữ liệu": explorer.render,
    },
    key="data_platform",
)
