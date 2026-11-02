"""Trang Điều phối: cảnh báo lô hàng sắp trễ hoặc đã trễ, và tra cứu trạng thái lô hàng."""

import rolepage
from views import alerts, shipments

rolepage.run(
    title="Điều phối",
    audience="Dispatcher và Customer Service",
    brs="BR-02, BR-03",
    views={
        "Cảnh báo trễ hạn": alerts.render,
        "Tra cứu lô hàng": shipments.render,
    },
    key="dispatch",
)
