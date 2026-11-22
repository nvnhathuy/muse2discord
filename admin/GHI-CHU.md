# Ghi chú bàn giao — trang quản trị hub

Người dựng: deputy, 2026-11-22. Luồng đã chốt: deputy dựng + tự chạy test
local đạt xanh → thả kết quả vào thư mục này → lead kiểm chứng độc lập
(chạy test, render thật + bấm thử, quét thông tin cá nhân) → báo chủ dự án
→ lead đẩy lên GitHub repo muse2discord dạng nhánh + PR (không tự merge,
không triển khai lên hub đang chạy).

## Đã làm

- Toàn bộ gói `hub_admin/` (Python stdlib thuần, không thêm dependency)
  + trang `static/admin.html` một file + 40 test (`tests/`), chạy xanh
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
5. **Phạm vi quyền tạm (đã sửa theo chốt 09:50 + 09:51 ngày 22/11)**:
   có 3 mức read / operate / full. Mức **full là toàn quyền như admin,
   gồm cả thao tác nhạy cảm** ở mục 6 (bản đầu khoá cứng việc này —
   đã GỠ theo lệnh đại ca); người nắm quyền full làm việc nhạy cảm
   không cần header xác thực lại, vì quyết định cấp chính là bước uỷ
   quyền của admin, và mọi thao tác vẫn ghi audit theo grant id. Ngoại
   lệ giữ cứng: **cấp/liệt kê/thu hồi grant chỉ phiên admin làm được**
   — `issue_grant`/`revoke_grant`/`list_grants` kiểm tra
   `identity.kind == "admin"`, không chấp nhận xác thực bằng grant ở
   bất kỳ mức nào, nên không có uỷ quyền chuỗi hay tự gia hạn. Test đã
   đảo chiều tương ứng: `test_sensitive_ops_need_full_grant` (tầng mô
   hình) và `test_full_grant_is_admin_equivalent_but_cannot_issue_grants`
   (tầng API, kiểm cả 2 mặt).

## Bổ sung vòng 10:01 ngày 22/11 (mật khẩu admin — 3 việc)

- **Đổi mật khẩu trong trang**: mục "Tài khoản quản trị" — nhập mật
  khẩu hiện tại + mật khẩu mới; sai mật khẩu hiện tại bị chặn (test có).
  Mật khẩu lưu băm PBKDF2-SHA256 600k vòng (`hub_admin/credentials.py`),
  không lưu thô ở bất cứ đâu. Đổi xong **huỷ mọi phiên khác**, giữ phiên
  của người vừa đổi.
- **Quên mật khẩu (đường chính, không SSH)**: admin tạo trước **mã khôi
  phục** trong trang (hiện một lần, server chỉ giữ băm, tạo mã mới thì
  mã cũ chết). Ở màn đăng nhập: nhập mã + mật khẩu mới → đặt lại xong
  mã bị đốt (dùng lại lần 2 bị chặn — test có), mọi phiên cũ bị huỷ,
  ghi audit `password_reset_recovery`. **Chỉnh theo chốt 10:07**: reset
  thành công thì server **sinh ngay mã khôi phục MỚI** và trả về để
  trang hiện một lần ở màn xác nhận — tài khoản không bao giờ trống mã
  dự phòng; test kiểm đủ: mã mới dùng được, mã cũ không dùng lại được.
- **Quên mật khẩu (dự phòng qua SSH, chính admin tự làm)**:
  `python3 -m hub_admin.reset_password --state-dir <dir>` — hỏi mật
  khẩu mới 2 lần (nhập ẩn), xong in xác nhận; kèm
  `HUONG-DAN-RESET-MAT-KHAU.md` tiếng Việt từng bước (SSH vào → chạy
  lệnh → đăng nhập lại → tạo mã khôi phục mới), toàn thông tin dạng
  placeholder, không có địa chỉ máy nhà thật.
- **Cơ chế huỷ phiên**: credentials có `version`, tăng mỗi lần đặt mật
  khẩu; phiên đăng nhập đóng dấu version lúc login và tự chết ở lần
  kiểm tra kế tiếp khi version lệch — nên cả 3 đường (đổi trong trang /
  mã khôi phục / CLI) đều huỷ được phiên của tiến trình server đang
  chạy mà không cần restart. Test CLI chạy cả hàm lẫn tiến trình con
  thật (`python3 -m hub_admin.reset_password`).
- **Ranh giới**: đổi mật khẩu và tạo mã khôi phục chỉ chấp nhận **phiên
  của chính admin** — grant mức full cũng bị từ chối (test có), cùng
  một đường ranh với việc cấp grant: thông tin xác thực của principal
  quản trị là của riêng người admin.
- Tương thích ngược: lần chạy đầu, nếu chưa có `admin_credentials.json`
  mà có token quản trị cũ (env/file), app tự lấy token đó làm mật khẩu
  ban đầu rồi chỉ dùng bản băm từ đó về sau.

## Kỷ luật đã tuân

- Không thông tin cá nhân thật trong code/test/mẫu: danh tính mẫu trung
  tính (alpha/beta/gamma/delta), không IP/ID/đường dẫn máy nhà thật.
- Chỉ code + test: không đụng container hub đang chạy, không triển khai.
- Mọi bí mật trong test đều là chuỗi giả.
