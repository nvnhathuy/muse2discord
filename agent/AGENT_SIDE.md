# Phần phía agent — cơ chế mở lại lính canh (bản giao việc phía agent)

Tài liệu này chép toàn bộ cơ chế phía agent của mẫu chuẩn để mọi agent áp
dụng nguyên bản. Các file trong gói này:

- `sentinel.sh` — lính canh đang chạy (một kết nối SSH bền + vòng lặp
  `bridge wait` phía VM).
- `portainer_bridge.py` — chạy TRÊN VM: đọc token Portainer tại chỗ
  (`/home/operator/.secrets/portainer_token`, không bao giờ rời hub host), gọi
  Portainer API của Docker host qua LAN, exec vào container `discord-hub-v2`, tách
  luồng docker (demux) và trả stdout/stderr/exit code như một lệnh thường.
  Nội dung `say` đi qua stdin được truyền bằng `--content-b64`.
- `connect_via_proxy.py` — ProxyCommand cho SSH: relay CONNECT qua proxy của
  runtime Muse ở cổng Tailscale 3130. Địa chỉ + thông tin xác thực của proxy
  đọc từ biến môi trường `HTTPS_PROXY` lúc chạy; KHÔNG có bí mật nào trong file.
- `say.sh` — lệnh trả lời của agent: một SSH thường cho mỗi lần trả lời
  (việc hiếm, một lần là xong), gọi `portainer_bridge.py say --final`.

## Sự thật nền tảng của runtime phía agent

Runtime của agent chỉ giao kết quả của một tiến trình nền cho agent KHI TIẾN
TRÌNH ĐÓ KẾT THÚC. Một tiến trình chạy vô tận không bao giờ đánh thức được
agent (bài học xương máu từ cầu v1: vòng lặp vô tận bắt được
tin trong vài giây nhưng agent không hề biết cho tới khi chủ phòng nhắn ở
kênh khác). Vì vậy toàn bộ thiết kế xoay quanh một điểm: **lính canh kết thúc
chính là tín hiệu đánh thức.**

## Luật đứng phía agent (đây là "bản giao việc" — nó nằm trong bộ nhớ dài hạn
## của agent, không có file riêng)

Mỗi khi tiến trình lính canh kết thúc (kết quả của nó được runtime giao lại
cho agent như một handoff), agent bắt buộc:

1. Đọc stdout của lính canh và xử lý theo đúng loại kết thúc:
   - JSON có `messages`: đây là việc. Vòng lặp phía VM đã gọi
     `bridge working` giữ chỗ cho tin đầu tiên ngay khi bắt được. Agent đọc
     tin, làm đúng việc được giao, trả lời bằng `say.sh`
     (`bridge say --reply-to <id> --final`; `--final` đồng thời là ack chốt).
     Ngữ cảnh (`context`) chỉ để biết tình hình, không trả lời.
   - `SENTINEL_MAX_LIFE` (lính canh đạt trần 2 giờ): không có việc, chỉ mở lại.
   - Lỗi vận chuyển (hub khởi động lại/chuyển đổi, SSH gãy, mạng chập) KHÔNG
     còn là một loại kết thúc nữa: lính canh tự nuốt lỗi, giãn nhịp thử lại
     15s → 30s → 60s (trần 60s) và canh tiếp vô hạn — nó phải sống sót qua
     các đợt hub nghỉ dài tùy ý (bài học 22/11/2026: lính canh cũ chết hẳn
     sau 3 lượt chờ lỗi trong lúc hub chuyển đổi ~2 phút, agent điếc tới khi
     có người mở lại tay). Agent vì vậy chỉ còn nhận hai loại kết thúc trên.
2. Ngay trong cùng lượt xử lý đó, mở lại lính canh bằng một tiến trình nền
   mới: `bash ~/workspace/discord-bridge-agent/sentinel.sh`.
   Không có ngoại lệ: xử lý xong mà chưa mở lại là hở canh.

## Sau khi runtime khởi động lại

Tiến trình nền KHÔNG sống qua khởi động lại của runtime/VM. Khi runtime hồi
phục, nó giao một handoff phục hồi cho phiên bị ngắt. Agent kiểm tra ba thứ
trước khi mở lại: (1) log của phiên lính canh bị ngắt (runtime giữ lại), (2)
đuôi file `hub_inbox.jsonl` phía agent — lính canh ghi JSON đã bắt vào đây
trước khi kết thúc, cho biết tin cuối đã xử lý tới đâu, (3) `ps` xem còn lính
canh nào sống không. Tin chưa ack không mất: hub giao lại cho lượt `wait` kế
tiếp (tối đa 3 lần), nên lính canh mới mở sẽ bắt lại ngay ở lượt đầu. Đã kiểm
chứng thực tế (runtime khởi động lại, không mất tin nào).

## Lưới an toàn — cron 5 phút của runtime

Ngoài cơ chế mở lại trong cùng lượt ở trên, agent còn dựng một job cron
của chính runtime của mình, chu kỳ 5 phút, tên
`<khoá-agent>-sentinel-watchdog` (prompt kết nối sinh khi thêm agent đã
dặn việc này ngay từ đầu). Mỗi lượt cron làm ba việc: (1) kiểm tra tiến
trình lính canh còn sống không — khớp tiến trình phải chính xác, theo
/proc hoặc mẫu duy nhất của script lính canh (bẫy kinh điển: `pgrep -f`
tự khớp chính lệnh kiểm tra và báo "còn sống" giả); chết thì dựng lại
ngay bằng đúng lệnh mở lính canh thường dùng. (2) Kiểm tra hàng đợi hub
bằng một lượt `bridge wait`; có tin chưa ack mà không lính canh nào
đang giữ thì xử lý luôn trong lượt cron đó. (3) Khoẻ và không có tin
thì kết thúc im lặng, không đăng gì. Cron KHÔNG thay lính canh — lính
canh vẫn là đường nhận tin chính, phản ứng trong vài giây; cron chỉ là
lưới an toàn để lính canh chết hẳn (runtime khởi động lại mà handoff
phục hồi không tới, tiến trình bị giết) thì agent vẫn sống lại trong
tối đa khoảng 5 phút.

## Các giới hạn đã biết (đều có trong code/ghi chú)

- Không dùng SSH ControlMaster: master kẹt ở bước tiền xác thực (CONNECT qua
  proxy) sẽ từ chối mọi phiên mux ("Master refused session request") trong
  khi SSH thường vẫn chạy tốt. Một kết nối SSH trơn là đủ.
- `bridge say` bắt buộc `--reply-to` là tin đã được route tới đúng agent;
  không đăng tin đứng riêng được.
- Một reply Discord tối đa 2000 đơn vị UTF-16 TÍNH GỘP qua các `--part`;
  code/nội dung dài đi qua file trên hub host, không lên bảng chat (luật phòng).
