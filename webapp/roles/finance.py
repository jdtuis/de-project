"""Trang Tài chính và Kế hoạch: lợi nhuận theo tuyến, nhu cầu và giá cước theo mùa."""

import rolepage
from views import route_profit, seasonal

rolepage.run(
    title="Tài chính và Kế hoạch",
    audience="Finance và Operations Planning",
    brs="BR-05, BR-06",
    views={
        "Lợi nhuận theo tuyến": route_profit.render,
        "Nhu cầu theo mùa": seasonal.render,
    },
    key="finance",
)
