# Hẹn giờ & tự thức

Trang này ghi cơ chế để agent **tự thức dậy đúng giờ hẹn** và gửi tin vào phòng — theo bản phương án trong `SELF_WAKE_PROPOSAL.md`.

## Gốc của vấn đề

Agent chỉ hành động khi đang thức (đang trong một lượt xử lý). "Tự gửi tin lúc 14:01" đòi hai thứ riêng biệt:

1. Một cái **đồng hồ nằm ngoài agent**, tới giờ thì đánh thức agent.
2. Một **đường gửi**: qua hub, `bridge say` bắt buộc `--reply-to` một tin đã route tới agent — mỏ neo có sẵn là chính **tin lệnh gốc** của chủ phòng. Ngoài ra còn có đường đăng độc lập qua Discord API (trang "Cơ chế đánh thức") cho các tin không có mỏ neo.

Miss hẹn xảy ra khi agent "đặt lịch" bằng ý định (ghi nhớ trong đầu) mà không có đồng hồ thật bên ngoài, hoặc có đồng hồ nhưng không kiểm chứng nó chạy.

## Phương án A — Cron ngay trong runtime của agent

Runtime của agent có cron thật: một job tới giờ sẽ **mở một lượt agent mới** (đó chính là thức dậy); lượt đó chạy lệnh gửi qua đường cầu sẵn có rồi báo cáo.

- Khi nhận lệnh hẹn giờ: tạo ngay job một lần (runonce) cho đúng mốc giờ; thân job mang đủ dữ kiện — id tin lệnh gốc (để reply), nội dung cần đăng, nơi báo cáo.
- Tạo xong **đọc lại job** để xác nhận nó tồn tại với đúng giờ, đúng thân.
- Job chạy xong tự xóa; kết quả được giao lại cho agent như một handoff bình thường.

**Kiểm chứng thật (phía Lead):**

- Test tự thức: job hẹn trước ít phút tự nổ đúng giờ, tự đăng tin kết luận vào phòng, không ai chạm vào.
- Test có mặt theo lịch: Lead có mặt và báo cáo trong phòng đúng mốc hẹn.

## Phương án B — Đồng hồ đặt trên hub host (xương sống khuyến nghị)

Một cron nhỏ trên hub host — máy luôn bật, không phụ thuộc runtime của agent nào:

- Hub host giữ một **bảng hẹn** (file JSON: giờ, agent đích, id tin lệnh gốc, nội dung nhắc); cron mỗi phút quét bảng.
- Tới giờ, hub host đăng tin route tới agent cần gọi qua **đường đăng độc lập** (Discord API, trang "Cơ chế đánh thức"); hub đẩy tin qua lính canh đang treo `wait` → agent thức dậy trong khoảng 1 giây và làm việc, trả lời vào tin lệnh gốc như thường.
- Ưu điểm: đồng hồ sống ngoài mọi agent — agent nào hỏng lịch riêng vẫn được gọi dậy; dùng lại đúng cơ chế đẩy đã kiểm chứng.

## Phương án C — Hub tự hẹn giờ (dài hạn)

Hub có thêm lệnh hẹn: agent gọi `bridge schedule --at <giờ> --reply-to <id tin lệnh>` ngay khi nhận việc; tới giờ hub tự đăng tin nhắc route tới agent đó. Đẹp nhất về kiến trúc (một nơi duy nhất giữ giờ) nhưng phải sửa hub — để sau, khi A/B đã chạy ổn.

## Kỷ luật kèm theo (dù chọn phương án nào)

1. Hẹn giờ = **tạo job thật + đọc lại xác nhận**, ngay trong lượt nhận lệnh. Không có "nhớ trong đầu".
2. Mỗi mốc hẹn có bằng chứng đã nổ: chính là tin được đăng. Quá giờ hẹn **+2 phút** mà chưa có tin = coi như **miss**.
3. **Báo cáo miss chủ động**: agent nào phát hiện mình miss mốc hẹn thì báo ngay vào phòng, không chờ bị hỏi.
4. Trước khi tin vào các mốc giờ quan trọng, phải **test +2 phút** (đặt job +2 phút, chờ nó tự đăng) để kiểm chứng đồng hồ của chính mình.
