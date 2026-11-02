"""Trang Đội xe và An toàn: bảng điểm hiệu suất xe và bảng điểm rủi ro tài xế."""

import rolepage
from views import fleet, safety

rolepage.run(
    title="Đội xe và An toàn",
    audience="Fleet Manager, Bảo trì và Safety Team",
    brs="BR-01, BR-07",
    views={
        "Hiệu suất đội xe": fleet.render,
        "An toàn tài xế": safety.render,
    },
    key="fleet_safety",
)
