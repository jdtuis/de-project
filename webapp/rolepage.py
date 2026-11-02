"""Khung chung của một trang theo nhóm người dùng: tiêu đề, người dùng mục tiêu, các chức năng chọn bằng nút radio.

Chỉ chức năng đang được chọn mới chạy mã và truy vấn, nên mở trang không kéo theo các truy vấn của chức năng khác."""

import streamlit as st

from common import get_creds


def run(title: str, audience: str, brs: str, views: dict, key: str):
    creds = get_creds()
    st.title(title)
    st.caption(f"Dành cho {audience}. Yêu cầu nghiệp vụ: {brs}.")
    names = list(views)
    choice = names[0]
    if len(names) > 1:
        choice = st.radio("Chức năng", names, horizontal=True, label_visibility="collapsed", key=f"view_{key}")
    st.divider()
    views[choice](creds)
