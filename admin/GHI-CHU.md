# Ghi chú bàn giao — trang quản trị hub

Người dựng: deputy, 2026-11-22. Luồng đã chốt: Chim dựng + tự chạy test
local đạt xanh → thả kết quả vào thư mục này → lead kiểm chứng độc lập
(chạy test, render thật + bấm thử, quét thông tin cá nhân) → báo chủ dự án
→ lead đẩy lên GitHub repo muse2discord dạng nhánh + PR (không tự merge,
không triển khai lên hub đang chạy).

## Đã làm

- Toàn bộ gói `hub_admin/` (Python stdlib thuần, không thêm dependency)
  + trang `static/admin.html` một file + 28 test (`tests/`), chạy xanh
  bằng `python3 -m unittest discover -s tests` trên máy người dựng, kèm smoke
  test chạy thật `python3 -m hub_admin.app` (seed từ env mẫu, đăng nhập,
  overview, trang 200).
- Đối chiếu đặc tả: mục 3 (hồ sơ đủ trường env hiện tại), 4.1 tổng quan,
  4.2 thêm agent (mặc định tạm dừng, checklist nằm ở form), 4.3 sửa
  (agent id bất biến, trùng dạng gọi bị chặn), 4.4 tạm dừng/mở lại hai
  mức — mức hub làm thật, mức agent chỉ hiển thị qua trường trạng thái
  runner do hub cấp qua `status_provider`, 4.5 xoá cứng (chặn khi còn
  tin chờ/lease; journal + audit không bị xoá), 4.6 hàng chờ (xem, giải
  phóng lease, ack hộ, reset cursor), 4.7 cấu hình vận hành (mặc định
  đúng số đo thật: lease 300/1800, giao lại tối đa 3, leo thang
  300/1800, circuit breaker 8), 4.8 journal (thống kê + báo cáo ngưỡng
  giữ 90 ngày / xoay 50MB — bản thân việc xoay vẫn là việc phía hub),
  mục 5 (principal riêng, phiên 30 phút), 5b (quyền tạm), 6 (xác thực
  lại cho 5 nhóm thao tác nhạy cảm), 8.1/8.2 (registry sửa nóng + API).

## Điểm Gà cần nối khi đưa vào cây muse2discord

1. **Nguồn trạng thái thật**: `DeliveryState` trong bản này là mô hình
   tham chiếu đúng ngữ nghĩa hub (cursor, giao lại, lease hết hạn). Khi
   tích hợp, thay bằng trạng thái thật của hub sau cùng một interface
   (`enqueue/claim_lease/ack/force_release/reset_cursor/probe/queue_for/
   cursor_of`) — registry và API không đổi.
2. **`status_provider`**: app.py đang dùng provider mặc định (ready,
   journal healthy, online rỗng). Nối vào nguồn của `bridge status` mở
   rộng theo mục 8.3 khi tích hợp.
3. **Bot online/offline khi thêm/thay token**: đặc tả yêu cầu hub thử
   đưa bot online ngay và báo kết quả trên trang. Bản này lưu token vào
   kho bí mật và registry có hiệu lực ngay; bước reconnect bot thuộc
   hub core (ngoài phạm vi gói này) — Gà nối sự kiện "token thay đổi /
   agent bật" từ registry vào vòng quản lý bot của hub.
4. **Xác thực lại (mục 6)**: bản này dùng cơ chế cắm được — mặc định là
   nhập lại token quản trị qua header `X-Admin-Reauth`. Khi triển khai
   thật, thay verifier trong `auth.py` bằng passkey/TOTP, API không đổi.
5. **Phạm vi quyền tạm**: grant gắn với agent nắm giữ (để audit), phạm
   vi read/operate áp cho thao tác trên bề mặt quản trị; thao tác nhạy
   cảm bị chặn cứng trong `grants.SENSITIVE_OPS` ở tầng mô hình, không
   chỉ ở API. Nếu đại ca muốn siết thêm "operate chỉ được đụng agent
   khác, không đụng chính mình" thì thêm điều kiện ở `api.authorize`.

## Kỷ luật đã tuân

- Không thông tin cá nhân thật trong code/test/mẫu: danh tính mẫu trung
  tính (alpha/beta/gamma/delta), không IP/ID/đường dẫn máy nhà thật.
- Chỉ code + test: không đụng container hub đang chạy, không triển khai.
- Mọi bí mật trong test đều là chuỗi giả.
