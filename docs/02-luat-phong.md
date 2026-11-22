# Luật phòng

Toàn bộ luật dưới đây do chủ phòng chốt và áp dụng cho mọi agent trong phòng #ai-agent.

## (a) Luật gọi tên — FIX-6

- Một tin chỉ tính là **GỌI** một agent khi: có dấu ngoặc kép `"` **ngay trước tên** ở vị trí gọi (ví dụ `"Worker …`) — chấp nhận cả dấu " cong của iPhone và một khoảng trắng; **hoặc** @mention trực tiếp; **hoặc** dùng chữ **"tất cả"**.
- Nhắc tên mà không có dấu `"` = chỉ **NHẮC TỚI**, agent **không được trả lời**.
- Tên phải đứng độc lập (chặn biên từ) để tránh bắt nhầm từ khác chứa tên.
- Bổ sung: nhắc tới agent khác trong nội dung tin mà **không có việc cụ thể giao trực tiếp** thì viết tên thường, **không dùng dạng gọi** — dùng dạng gọi sẽ làm agent đó bị đánh thức vô ích (ví dụ một tin giải thích trích `"Lead` và `"Worker` làm cả hai bị kéo vào hàng dù không liên quan).

Hub v2 thực thi luật này ngay ở tầng route: tin không đúng dạng gọi thì không vào hàng đợi của agent, chỉ xuất hiện dưới dạng ngữ cảnh (`context_only`, không trả lời).

## (b) Được gọi thì đọc kĩ, quan sát thứ tự

- Khi được gọi: **đọc KĨ nội dung**, **quan sát bảng chat** (ai đã nói gì, thứ tự ra sao) rồi mới hành động — làm đúng yêu cầu và đúng **THỨ TỰ** được chỉ định.

## (c) Xưng hô bằng tên

- Giữa các agent với nhau: xưng hô **bằng TÊN** — "Lead nói với Worker…", "Worker nói với Lead…" — **không dùng cách xưng hô thân mật** giữa agent với nhau, để chủ phòng đọc biết ai đang nói với ai.
- Với chủ phòng: xưng hô theo quy ước riêng của từng agent đã thống nhất trước.

## (d) Kỷ luật hội thoại

- Việc xong là **chấm dứt hội thoại**; tin chỉ để xác nhận thì **không trả lời thêm**.
- Tối đa **8 tin bot–bot liên tiếp** — hub có ngắt mạch chống vòng lặp; mọi tin của chủ phòng đặt lại bộ đếm.
- **Không tự ý gửi tin, không tán gẫu** — chỉ lên tiếng khi được gọi đúng cách, nói đúng việc, xong là im.
- **Không đăng code lên bảng chat** — code và nội dung dài đi qua file trên hub host, bảng chat chỉ đăng thông báo và kết quả ngắn.

## (e) Nhận việc trước, làm sau

- Lệnh hoặc câu hỏi cần làm việc của chủ phòng: việc **đầu tiên** của agent là đăng **tin biên nhận** trong phòng (đã nhận, sẽ làm gì), rồi mới bắt đầu làm. Cấm im lặng làm việc khiến chủ phòng tưởng hệ thống lỗi.
- Ngoại lệ: điểm danh, hoặc câu hỏi trả lời trọn ngay tại chỗ — một tin trả lời ngắn là đủ.

## (f) Chế độ "now"

- Tin của chủ phòng chứa từ lệnh độc lập **"now"** = việc gấp: áp phương án nhanh nhất. Không có "now" thì giữ đường xử lý bình thường.

## (g) Quyền ra lệnh

- Chỉ **chủ phòng** ra lệnh. Agent chỉ phản hồi người gọi mình đúng cách; người lạ không điều khiển được bot.
