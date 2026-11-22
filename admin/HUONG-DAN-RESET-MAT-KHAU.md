# Hướng dẫn đặt lại mật khẩu trang quản trị hub (đường dự phòng qua SSH)

Tài liệu này dành cho **chính admin (chủ nhà)** khi quên mật khẩu trang
quản trị VÀ không còn mã khôi phục. Toàn bộ việc do admin tự làm trên
máy chủ — không cần nhờ agent nào SSH vào sửa hộ.

Chỉ dùng cách này khi cả hai đường kia đều tắc:

1. Còn nhớ mật khẩu → đăng nhập và tự đổi trong mục "Tài khoản quản trị".
2. Quên mật khẩu nhưng còn **mã khôi phục** → ở màn đăng nhập, mục
   "Quên mật khẩu?": nhập mã khôi phục + mật khẩu mới là xong. Ngay khi
   đặt lại thành công, trang hiện **mã khôi phục MỚI** — cất mã mới đó
   ngay (mã cũ đã chết hẳn), để không bao giờ rơi vào trạng thái không
   còn mã dự phòng.
3. Mất cả mã khôi phục → làm theo tài liệu này.

---

## Bước 1 — Mở cửa sổ lệnh (terminal)

- Windows: mở **PowerShell** (bấm phím Windows, gõ `powershell`, Enter).
- macOS: mở **Terminal** (trong Applications → Utilities).
- Linux: mở terminal như thường lệ.

## Bước 2 — SSH vào máy chủ đang chạy hub

Gõ lệnh sau (thay phần trong ngoặc bằng thông tin máy nhà của bạn):

```
ssh <ten-dang-nhap>@<dia-chi-may-chu>
```

Ví dụ dạng lệnh: `ssh user@192.168.x.x` hoặc qua tên miền nội bộ của nhà.
Nhập mật khẩu của máy chủ khi được hỏi (mật khẩu này là mật khẩu SSH của
máy, khác mật khẩu trang quản trị). Đăng nhập thành công khi thấy dấu
nhắc lệnh của máy chủ.

## Bước 3 — Chạy lệnh đặt lại mật khẩu

Vào thư mục chứa gói hub_admin (nơi đã triển khai trang quản trị), rồi
chạy đúng một lệnh:

```
python3 -m hub_admin.reset_password --state-dir <thu-muc-trang-thai>
```

- `<thu-muc-trang-thai>` là thư mục trạng thái của trang quản trị (nơi
  chứa file `admin_credentials.json`). Nếu trang quản trị chạy với biến
  môi trường `HUB_ADMIN_STATE_DIR` thì có thể bỏ qua `--state-dir`,
  lệnh tự lấy theo biến đó; mặc định là `./state` tính từ thư mục hiện tại.

Chương trình sẽ hỏi:

```
Mat khau quan tri moi:
Nhap lai mat khau moi:
```

Gõ mật khẩu mới (ít nhất 8 ký tự — nên dài và khó đoán), Enter, rồi gõ
lại y hệt lần nữa. Khi gõ, màn hình **không hiện ký tự** — đó là bình
thường, cứ gõ hết rồi Enter.

## Bước 4 — Kiểm tra kết quả

Đúng thì chương trình in ra:

```
Da dat lai mat khau quan tri (luu bam tai .../admin_credentials.json).
Tat ca phien dang nhap cu da bi huy. Hay dang nhap lai bang mat khau moi.
```

Nếu báo "Hai lan nhap khong giong nhau" thì hai lần gõ chưa khớp — chạy
lại Bước 3, chưa có gì bị thay đổi cả.

## Bước 5 — Đăng nhập lại và làm ngay 2 việc

1. Mở trang quản trị, đăng nhập bằng mật khẩu mới vừa đặt.
2. Vào mục **"Tài khoản quản trị"** → **"Tạo mã khôi phục mới"** → cất
   mã hiện ra vào nơi an toàn (mã chỉ hiện một lần). Lần sau quên mật
   khẩu thì dùng mã này ở màn đăng nhập, khỏi cần SSH nữa.

## Lưu ý an toàn

- Mật khẩu chỉ lưu dạng **băm** (PBKDF2) trên máy chủ — lệnh này không
  in ra và không ai đọc ngược được mật khẩu từ file.
- Đặt lại mật khẩu xong, **mọi phiên đăng nhập cũ đều bị huỷ** ngay —
  nếu có ai đang giữ phiên cũ cũng bị đẩy ra.
- Đừng gửi mật khẩu mới qua chat/email cho bất kỳ ai, kể cả agent.
