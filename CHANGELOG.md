# Nhật ký thay đổi — Cầu Discord v2

Mới nhất ở trên cùng.

## [Bản đang chạy — 21/11/2026]

PR #9 đã merge (merge commit `8e2b4b9e`) và hub trên NAS đã dựng lại theo đúng bản merge này (image `discord-hub-v2:pr9`). Trạng thái sau khi dựng lại: 3 bot ga/lu/chim online, `bridge status` trả `ready=true`, journal healthy.

PR #9 gồm **4 vòng sửa** theo rà soát ở issue #7 và issue #10 (Claude và GPT cùng rà soát), kiểm chứng độc lập từng vòng; bộ test cuối đạt **159/159**. Các thay đổi chính:

- Biến tùy chọn rỗng trong `hub.env` không còn làm sập hub.
- Agent không còn tự route tin vào chính mình.
- Lính canh gia hạn lease cho **mọi** tin đang xử lý, không chỉ tin đầu tiên.
- Phanh theo agent hoạt động đúng: vô hiệu một agent qua `AGENT_KEYS`/token thì hub vẫn chạy với các agent còn lại và in dòng `agents summary` (active/disabled).
- Trần lease 30 phút tính theo **đồng hồ tường**, ghi bền trong journal — hub khởi động lại không cấp lại trần mới.
- Cảnh báo dự phòng ghi rõ tác giả; sửa tin cảnh báo không được thì đăng tin mới thay thế.
- Thêm lệnh `bridge status` **chỉ đọc** phục vụ health check (không nuốt tin thật như cách dùng `bridge wait` thăm dò trước đây).
- Regex FIX-6 siết lại: dấu ngoặc kép phải ở **đúng vị trí mở lời gọi** mới tính là gọi tên (trích dẫn giữa câu không còn kích hoạt nhầm).
- `portainer_bridge.py` tự đọc file cấu hình `~/.portainer_bridge.env` cạnh nó, lính canh không còn bị cắt đứt vì thiếu biến môi trường.
- `PORTAINER_BASE_URL` **bắt buộc** ở cả phía agent lẫn bộ deploy: thiếu hoặc rỗng thì báo lỗi cấu hình rõ ràng, hết mặc định lặng lẽ về localhost.
- Cấm agent key `hub` (tên dành riêng cho hệ thống).
- Đường dẫn `~` trong biến môi trường được `expanduser` đúng.
- `deploy/switch.py`: rollback **chỉ dừng v2 khi chính lượt chuyển đổi đó đã khởi động mới v2** — không dừng nhầm hub v2 vốn đang chạy từ trước.

## [v2.0.0 — 21/11/2026]

Bản v2 đầu tiên, thay toàn bộ nội dung cũ của repo:

- Cấu trúc mới: `hub/` (package `bridgehub` + CLI `bridge`), `agent/` (gói phía agent mẫu), `deploy/` (script triển khai qua Portainer), `docs/` (tài liệu vận hành — chính là sách BookStack "Cầu Discord" v2).
- README song ngữ Việt/Anh, có mục **Tailscale bắt buộc** (đường truy cập duy nhất vào nhà cho agent ở ngoài mạng).
- Hợp đồng §3: ack 2 mức (`working` / chốt), long-poll 50 giây, giao lại tối đa 3 lần, cảnh báo vào phòng sau 5 phút và @chủ phòng sau 30 phút, dead-man's switch.
- Gắn thẻ `v2`. Codex xây, bàn giao ở mức 107 test đạt.
