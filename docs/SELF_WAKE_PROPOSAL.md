# Phương án agent tự thức dậy gửi tin Discord theo lịch hẹn
Bản đề xuất phương án (soạn bởi Deputy), sau khi đã xảy ra các lần miss task hẹn giờ.

## Gốc của vấn đề
Agent chỉ hành động khi đang thức (đang trong một lượt xử lý). "Tự gửi tin lúc
14:01" đòi hai thứ riêng biệt:
1. Một cái ĐỒNG HỒ nằm ngoài agent, tới giờ thì ĐÁNH THỨC agent.
2. Một đường GỬI: qua hub, `bridge say` bắt buộc `--reply-to` một tin đã route
   tới agent — tin đứng riêng không đăng được. Mỏ neo có sẵn: chính TIN LỆNH
   của chủ phòng (tin đã route tới agent khi giao việc) — cách này đã được
   dùng và kiểm chứng ở các test giờ. Mọi tin hẹn giờ đều trả lời vào tin lệnh gốc.

Miss task xảy ra khi agent "đặt lịch" bằng ý định (ghi nhớ/sẽ làm) mà không có
đồng hồ thật bên ngoài, hoặc đồng hồ có nhưng không kiểm chứng nó chạy.

## Phương án A — Cron ngay trong runtime của agent (chính chủ)
Runtime của agent có cron thật: một job tới giờ sẽ MỞ MỘT LƯỢT AGENT MỚI (đó
chính là thức dậy), lượt đó chạy lệnh gửi qua đường cầu sẵn có rồi báo cáo.
- Khi nhận lệnh hẹn giờ: tạo ngay job một lần (runonce) cho đúng mốc giờ; phần
  thân job mang đủ dữ kiện: id tin lệnh gốc (để reply), nội dung cần đăng, nơi
  báo cáo. Tạo xong ĐỌC LẠI job để xác nhận nó tồn tại với đúng giờ đúng thân.
- Job chạy xong tự xóa; kết quả của job được giao lại cho agent như một
  handoff bình thường.
- Điều kiện: runtime của agent đó phải có cron tự đánh thức thật. Phải kiểm
  chứng bằng một test 2 phút (đặt job +2 phút, chờ nó tự đăng) TRƯỚC khi tin
  vào các mốc giờ quan trọng. Các lần miss hẹn cho thấy bước kiểm chứng này
  từng bị bỏ qua: lịch "đã đặt" nhưng không ai xác nhận nó nổ.

## Phương án B — Đồng hồ đặt trên hub host (bên ngoài mọi agent, chắc nhất)
Một cron nhỏ trên hub host (máy luôn bật, không phụ thuộc runtime của agent
nào): tới giờ hẹn, nó đăng một tin vào phòng route tới agent cần gọi (hoặc
ghi một tin "đến giờ làm việc X" kèm id tin lệnh gốc). Hub đẩy tin đó qua
lính canh đang treo `wait` → agent thức dậy trong ~1 giây và làm việc, trả
lời vào tin lệnh gốc như thường.
- Ưu điểm: đồng hồ sống ngoài agent, agent nào hỏng lịch riêng vẫn được gọi;
  dùng lại đúng cơ chế đẩy đã kiểm chứng.
- Cần phía giữ hạ tầng (hub host + hub) dựng: một bảng hẹn (file JSON trên hub host: giờ, agent,
  id tin lệnh gốc, nội dung nhắc) + cron mỗi phút quét bảng, tới giờ thì đăng
  tin qua cùng đường Portainer/bridge phía VM, đăng xong đánh dấu đã gọi.
- Đây là phương án được khuyến nghị làm xương sống, vì nó không tin vào đồng
  hồ riêng của bất kỳ agent nào.

## Phương án C — Hub tự hẹn giờ (lâu dài, việc của đội hub)
Hub có thêm lệnh hẹn: agent gọi `bridge schedule --at <giờ> --reply-to <id
tin lệnh>` ngay khi nhận việc; tới giờ hub tự đăng tin nhắc route tới agent
đó. Đẹp nhất về kiến trúc (một nơi duy nhất giữ giờ), nhưng phải sửa hub —
để sau, khi A/B đã chạy ổn.

## Kỷ luật kèm theo (dù chọn phương án nào)
1. Hẹn giờ = tạo job thật + đọc lại xác nhận, ngay trong lượt nhận lệnh.
   Không có "nhớ trong đầu".
2. Mỗi mốc hẹn có bằng chứng đã nổ: chính là tin được đăng. Quá giờ hẹn
   +2 phút mà chưa có tin = coi như miss.
3. Báo cáo miss chủ động: agent nào phát hiện mình miss mốc hẹn thì báo ngay
   vào phòng, không chờ bị hỏi.

## Kết luận
- Ngắn hạn: mỗi agent tự kiểm chứng cron của mình bằng test +2 phút ngay khi
  áp dụng; ai không có cron tự thức thì khai rõ.
- Xương sống: dựng phương án B trên hub host (phía giữ hạ tầng phụ trách) — đồng hồ ngoài
  agent, gọi dậy qua hub, không phụ thuộc runtime nào.
- Dài hạn: cân nhắc C khi sửa hub đợt tới.
