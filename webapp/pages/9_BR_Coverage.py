"""Ma trận yêu cầu nghiệp vụ: mỗi BR được đáp ứng ở trang nào, và còn thiếu gì."""

import pandas as pd
import streamlit as st

st.title("Yêu cầu nghiệp vụ")
st.caption("Mỗi yêu cầu nghiệp vụ (BR) nằm ở trang nào, đáp ứng tiêu chí thành công đến đâu và còn thiếu gì.")

FULL, PART = "Đáp ứng", "Đáp ứng một phần"

BRS = [
    dict(
        id="BR-01", name="Hiệu suất đội xe và bảo trì dự đoán", who="Fleet Manager, Bảo trì",
        page="pages/1_Fleet_Performance.py", where="Đội xe và An toàn > Hiệu suất đội xe", status=PART,
        how="Bảng hiệu suất từng xe theo tháng (giao đúng hạn, MPG, quãng đường) so với median đội xe, gắn cờ nhóm thấp nhất.",
        success="Xác định 10% xe kém nhất mỗi tháng: đạt.",
        missing="Mục tiêu giảm 15% sự cố hỏng xe sau 3 tháng chưa đo được, vì cần dữ liệu hỏng xe và bảo trì theo thời gian.",
    ),
    dict(
        id="BR-02", name="Theo dõi lô hàng thời gian thực", who="Điều phối, Customer Service",
        page="pages/2_Shipment_Alerts.py", where="Vận hành lô hàng > Cảnh báo trễ hạn", status=PART,
        how="Bảng cảnh báo At risk và Breached kèm số phút trễ hoặc còn lại, tự làm mới mỗi 30 giây, có chế độ phát lại lịch sử.",
        success="Phát hiện vi phạm trong 5 phút: phụ thuộc độ trễ của pipeline streaming.",
        missing="Trang hiển thị tuổi của sự kiện mới nhất để theo dõi độ trễ, nhưng chưa tự đo độ trễ từ lúc sự kiện xảy ra đến lúc phát hiện.",
    ),
    dict(
        id="BR-03", name="Góc nhìn dữ liệu vận hành thống nhất", who="Vận hành, Data Platform",
        page="pages/3_Unified_Shipment_View.py", where="Vận hành lô hàng > Tra cứu lô hàng", status=PART,
        how="Mô hình lô hàng một dòng một chuyến, đã nối khách hàng, tuyến, xe, tài xế, cơ sở, giao hàng và nhiên liệu. 8 câu hỏi chuẩn trả lời bằng một truy vấn.",
        success="Các câu hỏi chuẩn trả lời được không cần JOIN thêm: đạt với 8 câu đã định nghĩa.",
        missing="BR yêu cầu Silver layer làm mới hằng ngày. Hiện mô hình được dựng trên warehouse (mô hình sao) thay vì Silver layer trong lakehouse.",
    ),
    dict(
        id="BR-04", name="Kiểm soát chất lượng và nhất quán dữ liệu", who="Data Platform",
        page="pages/4_Data_Quality.py", where="Dữ liệu > Chất lượng dữ liệu", status=PART,
        how="Quét toàn bộ dòng với quy tắc khóa thiếu, khóa mồ côi, trùng lặp, giá trị và định dạng không hợp lệ, nhất quán. Mỗi quy tắc cho PASS hoặc FAIL, có thể ghi log vào dq_log.",
        success="100% bản ghi được đánh giá theo quy tắc, mỗi quy tắc có kết quả PASS hoặc FAIL: đạt cho dữ liệu trong warehouse.",
        missing="Chưa kiểm tra trước khi dữ liệu vào bảng báo cáo, và chưa có bản ghi corrected hoặc quarantined. Phần này phải làm trong job Spark.",
    ),
    dict(
        id="BR-05", name="Phân tích lợi nhuận theo tuyến", who="Vận hành, Tài chính",
        page="pages/5_Route_Profitability.py", where="Doanh thu và Khách hàng > Lợi nhuận theo tuyến", status=FULL,
        how="Doanh thu, nhiên liệu, bảo trì phân bổ theo dặm, doanh thu/dặm, lợi nhuận và biên lợi nhuận theo tuyến và kỳ. Có tab độ phủ chi phí.",
        success="Doanh thu và chi phí gắn được vào tuyến: đạt, và có số liệu độ phủ để kiểm chứng.",
        missing="Chỉ có chi phí nhiên liệu và bảo trì. Lương tài xế, phí cầu đường, bảo hiểm và khấu hao không có trong dữ liệu, nên lợi nhuận cao hơn thực tế.",
    ),
    dict(
        id="BR-06", name="Phân tích nhu cầu và giá cước theo mùa", who="Quản lý, Vận hành, Tài chính",
        page="pages/6_Seasonal_Demand.py", where="Doanh thu và Khách hàng > Nhu cầu theo mùa", status=FULL,
        how="Số lô, doanh thu, giá cước và cơ cấu booking theo tháng, chia theo tuyến, khách hàng hoặc loại booking, kèm chỉ số mùa vụ.",
        success="Tổng hợp nhất quán theo tháng và so sánh được giữa tuyến, khách hàng, loại booking: đạt.",
        missing="Số booking được hiểu là số lô theo loại booking vì chưa có bảng booking riêng. Chỉ số mùa vụ cần ít nhất 12 tháng dữ liệu.",
    ),
    dict(
        id="BR-07", name="Bảng điểm an toàn và rủi ro tài xế", who="Safety, Fleet Manager",
        page="pages/7_Driver_Safety.py", where="Đội xe và An toàn > An toàn tài xế", status=FULL,
        how="Số sự cố, tỷ lệ lỗi tài xế, tỷ lệ phòng tránh được và chi phí thiệt hại mỗi tài xế theo tháng, gắn cờ nhóm rủi ro cao nhất.",
        success="Xác định 10% tài xế rủi ro cao nhất mỗi tháng: đạt. Giảm sự cố phòng tránh được sau 3 tháng: theo dõi được ở tab Xu hướng.",
        missing="Cột chi phí cộng vehicle_damage_cost, cargo_damage_cost và claim_amount, có thể tính trùng nếu claim đã gồm thiệt hại.",
    ),
    dict(
        id="BR-08", name="Tập trung doanh thu và giữ chân khách hàng", who="Sales, Tài chính",
        page="pages/8_Customer_Revenue.py", where="Doanh thu và Khách hàng > Doanh thu khách hàng", status=FULL,
        how="Doanh thu, số lô, giá cước và tỷ trọng theo khách hàng và tháng, kèm hồ sơ tài khoản. Đo tập trung (top 1, top 5, top 20%, HHI) và cảnh báo suy giảm.",
        success="Mọi khách hàng hoạt động có xu hướng theo tháng, và tỷ trọng doanh thu của 20% khách hàng đầu đo được mỗi tháng: đạt.",
        missing="Khách hàng chưa gán trong dữ liệu chuyến đi được báo riêng, và làm lệch chỉ số tập trung nếu tỷ lệ này cao.",
    ),
]

n_full = sum(b["status"] == FULL for b in BRS)
c1, c2, c3 = st.columns(3)
c1.metric("Tổng số BR", len(BRS))
c2.metric("Đáp ứng", n_full)
c3.metric("Đáp ứng một phần", len(BRS) - n_full)

table = pd.DataFrame([{
    "BR": b["id"], "Yêu cầu": b["name"], "Người dùng": b["who"], "Vị trí trong ứng dụng": b["where"], "Mức đáp ứng": b["status"],
} for b in BRS])
st.dataframe(table, width="stretch", hide_index=True)

st.subheader("Chi tiết từng yêu cầu")
for b in BRS:
    with st.expander(f"{b['id']}: {b['name']} ({b['status']})"):
        st.write(f"**Đáp ứng bằng:** {b['how']}")
        st.write(f"**Tiêu chí thành công:** {b['success']}")
        st.write(f"**Còn thiếu hoặc lưu ý:** {b['missing']}")
        st.page_link(b["page"], label=f"Mở trang ({b['where']})")
