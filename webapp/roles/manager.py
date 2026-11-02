"""Trang Quản lý: chỉ số chính của toàn hệ thống, xu hướng theo mùa và mức đáp ứng các yêu cầu nghiệp vụ."""

import rolepage
from views import br_coverage, overview, seasonal

rolepage.run(
    title="Quản lý",
    audience="Ban quản lý",
    brs="tổng hợp 8 BR, BR-06",
    views={
        "Tổng quan": overview.render,
        "Nhu cầu theo mùa": seasonal.render,
        "Yêu cầu nghiệp vụ": br_coverage.render,
    },
    key="manager",
)
