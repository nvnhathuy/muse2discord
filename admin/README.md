# Trang quản trị hub — bản dựng theo đặc tả

Bản dựng đầy đủ theo `dac-ta-trang-quan-tri-hub.md` (đặc tả trang quản trị
hub, đã chốt đủ 3 quyết định ở mục 10 ngày 22/11/2026):

- **Hồ sơ agent là trạng thái sửa nóng của hub** (mục 8.1): thêm / sửa /
  tạm dừng / mở lại / xoá cứng có hiệu lực ngay từ file trạng thái, không
  dựng lại container, không restart. Lần đầu chạy, registry tự nhập từ
  cấu hình env cũ (`AGENT_KEYS` + `<KEY>_*`) để di trú không mất đội hình.
- **API quản trị riêng** (mục 8.2) qua principal quản trị tách biệt,
  **không** qua bridge CLI của agent.
- **Trang quản trị địa chỉ riêng** (mục 5): một trang duy nhất — tổng
  quan, agent, hàng chờ, quyền tạm, cấu hình vận hành, nhật ký.
- **Quyền tạm thời cho agent** (mục 5b): agent không có quyền thường
  trực; quản trị viên cấp token tạm theo phạm vi (read / operate) và
  thời hạn (mặc định 60 phút, tối đa 24 giờ), thu hồi hiệu lực ngay.
  Thao tác nhạy cảm (mục 6) không bao giờ cấp tạm được.
- **Token bot không bao giờ hiển thị**: chỉ "đã có / chưa có + độ dài +
  thời điểm cấp", giá trị nằm ở kho bí mật riêng (file 0600).
- **Mọi thao tác ghi audit** append-only, gắn grant id khi agent làm
  bằng quyền tạm.

## Chạy thử

```bash
HUB_ADMIN_TOKEN=<token-quan-tri> python3 -m hub_admin.app
# mở http://127.0.0.1:8899/
```

Biến môi trường: xem docstring trong `hub_admin/app.py`. File mẫu trung
tính: `sample/sample.env`.

## Kiểm thử

```bash
python3 -m unittest discover -s tests -v   # 28 test, chỉ dùng stdlib
```

## Cấu trúc

| File | Vai trò |
|---|---|
| `hub_admin/registry.py` | Hồ sơ agent sửa nóng + seed từ env + chốt xoá cứng |
| `hub_admin/secrets_store.py` | Kho token bot (chỉ metadata lộ ra ngoài) |
| `hub_admin/grants.py` | Quyền tạm: cấp / thu hồi / hết hạn / phạm vi |
| `hub_admin/audit.py` | Audit append-only, lọc theo agent/thao tác/thời gian |
| `hub_admin/ops_config.py` | Cấu hình vận hành (mục 4.7), mặc định = số đo thật của hub |
| `hub_admin/delivery.py` | Mô hình hàng chờ/lease/cursor theo đúng ngữ nghĩa hub |
| `hub_admin/journal.py` | Thống kê journal + báo cáo chính sách giữ/xoay (mục 4.8) |
| `hub_admin/auth.py` | Principal quản trị: phiên 30 phút + xác thực lại cho thao tác nhạy cảm |
| `hub_admin/api.py` | HTTP API + phục vụ trang |
| `hub_admin/static/admin.html` | Trang quản trị (một file, không phụ thuộc ngoài) |
| `hub_admin/app.py` | Điểm chạy: nối các phần + di trú token env vào kho bí mật |
