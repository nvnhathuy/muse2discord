# Tổng quan & kiến trúc

Phiên bản tài liệu: v2

Phòng **#ai-agent** là nơi chủ phòng điều phối **3 agent cùng lúc**: ra lệnh một chỗ, đọc cả ba phối hợp và báo cáo ngay trong phòng.

- Kênh: `#ai-agent` (ID `YOUR_CHANNEL_ID`), server Discord (guild `YOUR_GUILD_ID`).
- Chủ phòng (OWNER_ID `YOUR_OWNER_ID`) — người duy nhất ra lệnh.

## Kiến trúc v2: một hub trung tâm

Khác v1 (mỗi agent một container cầu riêng, chuyển tin bằng file inbox/outbox), v2 gom cả ba bot vào **một tiến trình hub duy nhất**:

- Container **`discord-hub-v2`** chạy trên Docker host (image `discord-hub-v2:trial`, user `10001:10001`, restart `unless-stopped`).
- Hub giữ cả 3 kết nối bot Discord (mỗi agent vẫn là **một ứng dụng bot riêng**), hàng đợi trong bộ nhớ cho từng agent, và một **nhật ký bền vững** dạng JSONL tại `/data/bridge.jsonl` (bind `bridge-v2-data:/data`).
- Agent nói chuyện với hub qua **Unix socket** `/run/bridge.sock` (bind `bridge-v2-run:/run`) bằng CLI `bridge` — chi tiết ở trang "Cơ chế đánh thức".
- Dữ liệu cấu hình (token bot, ID, hạn nhận) nạp từ file `hub.env` ngoài repo, chmod 600. Token không đi qua agent nào khác.

| Agent | App Discord (bot ID) | Hạn nhận tin |
|---|---|---|
| Lead | `YOUR_BOT_ID` | 180 giây |
| Worker | `YOUR_BOT_ID` | 300 giây |
| Deputy | `YOUR_BOT_ID` | 180 giây |

## Đường truy cập: tất cả qua Tailscale

Toàn bộ đường truy cập vào hệ thống chạy qua **Tailscale** — thiếu Tailscale là không dùng được:

- Hub chạy trên Docker host trong mạng nhà; cả 3 agent (Lead, Deputy, Worker) đều ở **ngoài mạng nhà**.
- Máy chủ chạy các script cầu phía máy chủ (hub host) có địa chỉ Tailscale **`<TAILSCALE_IP>`**. Các agent vào nhà qua Tailscale trước, rồi từ đó mới SSH tới hub host (địa chỉ này chính là đích SSH trong script lính canh: `operator@<TAILSCALE_IP>`) và đi tiếp qua LAN tới Portainer của Docker host / container hub.
- Trên máy của một số agent, đường Tailscale đi qua **proxy connector** của runtime (`connect_via_proxy.py` trong gói phía agent, dùng làm `ProxyCommand` cho SSH).

## Luồng tin nhắn

1. Hub nhận tin từ Discord, đối chiếu luật gọi tên (trang "Luật phòng"): tin chỉ vào hàng đợi của agent khi được **gọi đúng dạng**; các tin còn lại chỉ làm ngữ cảnh.
2. Lính canh phía agent (trang "Cơ chế đánh thức") đang treo `bridge wait` — có tin là phiên canh kết thúc và **đánh thức agent**.
3. Agent xử lý, gọi `bridge working` giữ chỗ khi bắt đầu việc dài, và trả lời bằng `bridge say --reply-to <id>`; `--final` chốt trao đổi.
4. Tin chưa chốt được hub **giao lại tối đa 3 lần**; quá 5 phút hub cảnh báo vào phòng, quá 30 phút leo thang @chủ phòng. Bảng trạng thái của hub tự đăng "escalation" / "recovered" theo trạng thái của từng agent.

## Đường quay lui

Nếu còn giữ cầu v1 (mỗi agent một container cầu file-based trên hub host), chúng được giữ nguyên làm đường quay lui: khi cần, dừng hub v2 và khởi động lại các container v1.

## Chi phí token

Bản thân hub **không tốn token AI** — nó chỉ chuyển tin. Token tốn ở lượt thức của agent phía sau, nên phòng có luật chống tán gẫu nghiêm (trang "Luật phòng").

## Nhật ký thay đổi v2

- Thay 3 container cầu file-based (inbox/outbox) bằng **một hub trung tâm** có hàng đợi bộ nhớ + nhật ký JSONL bền vững; tin chưa chốt được giao lại tối đa 3 lần thay vì mất khi bot offline.
- CLI phía agent chuẩn hóa còn 4 lệnh: `wait`, `say`, `ack`, `working`; có **ack 2 mức** (working = mức 1, say/ack chốt = mức 2) và lease gia hạn theo `working`.
- Đánh thức chuẩn hóa theo **một mẫu lính canh chung** cho cả 3 agent (trang "Cơ chế đánh thức"); bỏ hẳn đường hook thăm dò từng gây lỗi "đã like mà im lặng".
- Có **đường đăng tin độc lập** qua Discord API: agent đăng tin gọi/điểm danh/theo lịch được mà không cần tin mỏ neo (trang "Cơ chế đánh thức").
- Thêm cơ chế **hẹn giờ / tự thức** (trang "Hẹn giờ & tự thức") và phát hiện agent im lặng: cảnh báo phòng sau 5 phút, leo thang @chủ phòng sau 30 phút.
- Bộ test của hub đạt **112/112 test**.
