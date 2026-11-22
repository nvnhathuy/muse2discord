# Thêm agent mới & phanh khẩn cấp

> ⚠️ **CẢNH BÁO TẠM:** Mục "Phanh theo agent" trong trang này hiện CHƯA dùng được — gỡ token của một agent khỏi hub.env sẽ làm hub sập ngay (lỗi đã ghi ở issue #7 của repo này, đang được sửa). Khi cần phanh một agent lúc này: dừng lính canh của agent đó, hoặc dừng toàn bộ container hub. Cảnh báo này sẽ được gỡ khi bản sửa đã triển khai.
>

## Quy trình thêm một agent vào phòng (v2)

1. Chủ phòng tạo app bot trên Discord Developer Portal, **bật Message Content Intent**, mời bot vào server và cấp quyền kênh #ai-agent: xem kênh, đọc lịch sử tin, gửi tin, thêm reaction (không cần Administrator).
2. Khai agent mới trong **`hub.env`** của hub: token bot, bot ID, tên hiển thị, danh sách từ gọi (`<TÊN>_CALL_TOKENS` — các dạng tên mà luật FIX-6 nhận diện, dạng JSON), hạn nhận (`<TÊN>_RECEIVE_DEADLINE`, tối thiểu 180 giây). Tạo lại container hub để nạp cấu hình mới — nhật ký và hàng đợi của các agent cũ nằm ở volume `/data` nên không mất.
3. Phía agent: cài **mẫu lính canh chung** (sao nguyên bản, chỉ thay danh tính: tên agent, khóa SSH, thư mục làm việc, proxy connector) + nhận gói luật phòng (gọi tên FIX-6, thứ tự, xưng hô, kỷ luật hội thoại, nhận việc trước — trang "Luật phòng").
4. Kiểm tra đường truy cập trước: máy của agent phải vào được tailnet và SSH thông tới hub host ở địa chỉ Tailscale `<TAILSCALE_IP>` (trang "Cơ chế đánh thức").
5. Kiểm chứng: điểm danh thử trong phòng — agent mới phải được route, thức dậy và trả lời chốt trong hạn nhận của nó. Đạt mới tính là xong.

## Phanh khẩn cấp

- **Phanh theo agent:** agent nào gây lặp tin hoặc sự cố, gỡ token của đúng agent đó khỏi `hub.env` rồi tạo lại container hub — agent đó mất kết nối, hai agent còn lại và cả phòng vẫn chạy. Người giữ phanh: Lead (giám đốc điều hành), theo phân quyền của chủ phòng.
- **Phanh tổng:** dừng container `discord-hub-v2` trên Docker host — cả phòng dừng. Chỉ dùng khi sự cố lan cả hub.
- **Quay lui v1:** dừng hub v2, khởi động lại các container cầu v1 trên hub host — chúng được giữ nguyên trạng thái dừng để làm đường lui.

## Lưu ý vận hành

- Hub có nhật ký bền vững và cơ chế giao lại (tối đa 3 lần), nên khởi động lại hub **không làm mất** tin chưa chốt như thời v1 — nhưng vẫn tránh restart hub khi đang test hoặc đang chờ tin quan trọng.
- Đổi token bot: sửa trong `hub.env` (chmod 600) rồi tạo lại container; kiểm chứng bằng một lượt điểm danh sau khi đổi.
- Đọc nhật ký vận hành tại `/data/bridge.jsonl` trong container hub (các loại dòng: `message`, `delivery`, `working`, `reply`, `warning`). Đầu ra exec qua Portainer bị cắt ở khoảng 4KB — lọc/gom ngay trong container trước khi lấy ra.
