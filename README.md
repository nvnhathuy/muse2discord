# muse2discord — Hub điều phối agent qua Discord

> English summary at the bottom of this file: [English summary](#english-summary)

**muse2discord** là một hub trung tâm giúp nhiều agent (phiên Muse hoặc
chương trình gọi API) cùng làm việc trong **một phòng chat Discord**: chủ
phòng ra lệnh trong phòng, hub định tuyến tin tới đúng agent được gọi tên,
giữ hàng đợi bền vững khi agent vắng mặt, và ghi nhật ký toàn bộ hoạt động.
Kèm theo hub là một **trang quản trị web** để quản lý agent, cấp quyền tạm,
và kết nối agent mới — tất cả đóng gói trong **một container Docker duy
nhất**.

Nguyên tắc kiến trúc quan trọng nhất: **hub là trung tâm duy nhất**. Agent
không tự đăng nhập Discord, không tự đọc kênh chat. Mỗi agent chạy một
"lính canh" (sentinel) trên máy của mình; lính canh giữ một kết nối SSH tới
máy điều phối và treo lệnh `bridge wait` — hub đẩy tin tới qua đường đó
khi có tin được định tuyến cho agent. Agent trả lời bằng `bridge say`.
Nhờ vậy agent không cần token Discord, không cần quyền truy cập máy chủ
chat, và hub kiểm soát toàn bộ việc ai nhận tin gì.

```
                         ┌─────────────────────────────────────────────┐
                         │           CONTAINER HUB (1 cái)             │
                         │                                             │
   Phòng Discord  ⇄      │  Lõi hub (bridgehub)  ⇄  Trang quản trị     │
   (nhiều bot,           │  - giữ kết nối mọi bot   (cùng tiến trình,  │
    mỗi bot =            │  - định tuyến theo tên    cổng riêng)       │
    một agent)           │  - hàng đợi + nhật ký                       │
                         │  - registry agent (sửa nóng)                │
                         └───────▲──────────────────────┬──────────────┘
                                 │ socket /run/bridge.sock
                                 │ (qua SSH exec vào container)
              ┌──────────────────┴───────────────────┐
              │        Máy điều phối (VM)            │
              │        cổng exec vào container       │
              └──────────────────▲───────────────────┘
                                 │ SSH (LAN hoặc Tailscale)
                    ┌────────────┴────────────┐
                    │  Máy của từng agent     │
                    │  lính canh: bridge wait │
                    │  trả lời:   bridge say  │
                    └─────────────────────────┘
```

Cấu trúc repo:

| Thư mục | Nội dung |
|---|---|
| `hub/` | Lõi hub: package `bridgehub/`, tầng tích hợp `hub_integration/`, CLI `bridge`, test |
| `admin/` | Trang quản trị: package `hub_admin/` (API + trang web một file), test, hướng dẫn reset mật khẩu |
| `agent/` | Gói phía agent mẫu: `sentinel.sh` (lính canh), `say.sh`, `portainer_bridge.py`, `AGENT_SIDE.md` (luật phía agent) |
| `deploy-integrated/` | **Đường triển khai chính**: `Dockerfile` + `compose.yaml` cho bản tích hợp hub + trang quản trị, thư mục `keydrop/` cho tính năng tự thêm SSH key |
| `deploy/` | Script triển khai đời trước qua Portainer (chỉ còn tham khảo) |
| `docs/` | Tài liệu vận hành: luật phòng, phân cấp vai trò, cơ chế đánh thức, hẹn giờ |
| `tests_integration/` | Test tích hợp hub ⇄ trang quản trị |

---

## 1. Chức năng

### Lõi hub

- **Nhiều bot trong 1 container**: một tiến trình hub giữ kết nối của mọi
  bot Discord (mỗi bot = một agent), thêm bot mới không cần thêm container.
- **Định tuyến theo luật gọi tên**: một tin chỉ được giao cho agent khi
  chủ phòng *gọi* đúng cách — dấu `"` ngay trước tên agent, @mention,
  hoặc chữ "tất cả" cho các agent có bật nhận tin "tất cả". Nhắc tên suông
  không tính là gọi, tránh agent trả lời nhầm.
- **Hàng đợi + phát lại khi agent vắng**: tin giao cho agent đang ngoại
  tuyến được giữ trong nhật ký bền vững (`/data/bridge.jsonl`) và giao
  lại khi agent nối lại; mỗi tin được giao lại tối đa 3 lần nếu chưa chốt.
- **Xác nhận 2 mức**: `bridge working` = đã nhận (gia hạn thời gian xử
  lý), `bridge say`/`ack` = đã chốt. Quá 5 phút chưa xong hub cảnh báo
  trong phòng, quá 30 phút hub nhắc chủ phòng.
- **Ngắt mạch chống lặp**: tối đa 8 tin bot–bot liên tiếp, tránh hai agent
  tự nói chuyện vô hạn.
- **Ngữ cảnh kèm theo**: mỗi đợt giao tin kèm vài tin gần nhất trong
  phòng (số lượng cấu hình được) để agent nắm bối cảnh.

### Trang quản trị

Một trang web duy nhất (đăng nhập bằng mật khẩu admin), gồm:

- **Tổng quan**: trạng thái từng bot (sẵn sàng / tạm dừng), hàng chờ,
  thống kê nhật ký.
- **Quản lý agent**: thêm / sửa / **xoá cứng** hồ sơ agent, **nạp nóng**
  — thay đổi có hiệu lực ngay, không khởi động lại container. Hồ sơ gồm
  tên hiển thị, bot id, các dạng gọi tên, loại ghế (agent sống / ghế
  API), vai trò, hạn nhận–trả lời tin, ghi chú.
- **Tạm dừng 2 mức**: mức hub (nút Tạm dừng trong trang — chặn định
  tuyến ngay, bot vẫn online) và mức agent (phía agent tự dừng, hub
  không can thiệp).
- **Quyền tạm (grant)**: admin cấp cho agent một token tạm theo phạm vi
  **read / operate / full** và thời hạn (mặc định 60 phút, tối đa 24
  giờ). Mức full = toàn quyền như admin kể cả thao tác nhạy cảm, *ngoại
  lệ duy nhất*: không ai ngoài admin được cấp/thu hồi grant. Token chỉ
  hiện **một lần** lúc cấp; thu hồi có hiệu lực **ngay lập tức**; mọi
  thao tác bằng grant đều ghi audit kèm id của grant.
- **Mật khẩu admin**: đổi ngay trong trang. Quên mật khẩu thì dùng **mã
  khôi phục** ở màn đăng nhập — mã chỉ lưu dạng băm, dùng một lần, dùng
  xong trang **tự sinh mã mới**. Mất cả mật khẩu lẫn mã khôi phục thì
  đặt lại bằng CLI trên máy chủ (xem mục Tài liệu liên quan).
- **TOTP (tuỳ chọn)**: ghi danh mã xác thực 6 số trong trang; sau đó các
  thao tác nhạy cảm dùng mã TOTP thay cho việc nhập lại mật khẩu. Chưa
  ghi danh thì thao tác nhạy cảm xác thực bằng mật khẩu.
- **Tự đăng xuất** sau một khoảng không thao tác (cấu hình được), và nút
  Đăng xuất thủ công.
- **Cấu hình vận hành**: các thông số của hub (hạn nhận tin, số lần giao
  lại, mốc cảnh báo, thời gian tự đăng xuất…) sửa ngay trong trang, mỗi
  ô có ghi chú tiếng Việt ngay bên dưới.
- **Nhật ký & audit**: audit ghi lại mọi thao tác thay đổi trên trang
  (ai làm, làm gì, lúc nào, bằng phiên admin hay bằng grant nào), tải
  xuống được.
- **Sinh prompt kết nối**: ngay sau khi thêm agent, trang tự sinh một
  prompt đầy đủ để gửi cho agent — gồm thông tin kết nối, luật phòng và
  **code mẫu lính canh nguyên văn** (agent không phải tự viết lính
  canh). Prompt **không chứa token bot**.
- **Tự thêm SSH key cho agent (keydrop)**: agent gửi PUBLIC key, admin
  dán vào hộp thoại "SSH key" trong trang — trang tự thêm key lên máy
  điều phối qua một kênh SSH bị khoá chặt, hiện fingerprint + thời gian
  đã thêm. Không cần ai đăng nhập máy điều phối làm trung gian.
- **Agent mới mặc định ở trạng thái Tạm dừng** — chỉ nhận tin sau khi
  admin bấm "Mở lại".

### Phía agent

- `agent/sentinel.sh`: lính canh mẫu — một kết nối SSH duy nhất, vòng
  `bridge wait` phía máy điều phối (qua `portainer_bridge.py` exec vào
  container hub); có tin là đánh thức agent, agent xử lý xong mở lại
  lính canh ngay.
- `agent/say.sh`: mẫu lệnh trả lời (`bridge say --reply-to ... --final`).
- `agent/AGENT_SIDE.md`: luật ứng xử phía agent (luật gọi tên, kỷ luật
  trả lời, chống trùng).

---

## 2. Chuẩn bị

Bạn cần:

1. **Một máy chạy Docker** cho hub (Linux, Docker Engine + Compose v2).
2. **Một máy điều phối (VM)** có SSH — nơi lính canh của agent SSH tới
  và exec vào container hub. Máy này **có thể chính là máy chạy hub**
  nếu máy đó cho phép exec vào container (bản tham chiếu dùng
  Portainer API qua `agent/portainer_bridge.py`).
3. **Mạng nối ba phía**: máy của agent phải SSH được tới máy điều phối,
   máy điều phối tới được hub. Dùng LAN nếu ở cùng mạng, hoặc
   **Tailscale** nếu agent ở ngoài (cài Tailscale trên máy điều phối và
   máy của từng agent, đăng nhập cùng một tailnet, kiểm tra
   `ssh <user>@<VM_IP>` thông trước khi làm tiếp).
4. **Tài khoản Discord** và một server (guild) có một kênh phòng cho
   đội agent.
5. **Một ứng dụng bot Discord cho mỗi agent** (xem bước 2 khi triển
   khai).

Không cần: cơ sở dữ liệu riêng, Redis, hay dịch vụ ngoài nào khác —
toàn bộ trạng thái nằm trong thư mục dữ liệu `/data` của container.

---

## 3. Triển khai mới hoàn toàn

Mọi địa chỉ/token trong ví dụ dưới đây là **placeholder** — thay bằng
giá trị thật của bạn.

### Bước 1 — Lấy mã nguồn

```bash
git clone <DIA_CHI_REPO> muse2discord
cd muse2discord
```

(Kiểm chứng mã nguồn trước khi dựng, không cần Discord/token: chạy
`python3 -m pytest` trong `hub/`, `python3 -m unittest discover -s tests`
trong `admin/`.)

### Bước 2 — Tạo bot và thu thập ID

Với **mỗi agent**, trong [Discord Developer Portal](https://discord.com/developers/applications):

1. New Application → mục **Bot** → Reset Token, lưu token lại (đây là
   `<KEY>_BOT_TOKEN`, chỉ hiển thị một lần).
2. Bật **MESSAGE CONTENT INTENT** (mục Bot → Privileged Gateway Intents).
3. Mục **OAuth2 → URL Generator**: scope `bot`, quyền View Channel /
   Read Message History / Send Messages / Add Reactions → mở link mời
   bot vào server.

Thu thập các ID (bật Developer Mode trong Discord: Cài đặt → Nâng cao →
Developer Mode, rồi chuột phải để chép ID):

- `GUILD_ID`: chuột phải vào server → Copy Server ID.
- `CHANNEL_ID`: chuột phải vào kênh phòng → Copy Channel ID.
- `OWNER_ID`: chuột phải vào tên của bạn → Copy User ID.
- `<KEY>_BOT_ID`: Application ID của từng bot (trang General
  Information của ứng dụng).

### Bước 3 — Chuẩn bị file env

Tạo file `.env` cạnh `deploy-integrated/compose.yaml` (hoặc trỏ biến
`BRIDGE_ENV_FILE` tới file env của bạn), quyền `600`:

```bash
chmod 600 .env
```

Nội dung — khai đội hình ban đầu (sau lần chạy đầu, danh sách agent
được hub nhập vào registry trong `/data` và từ đó **trang quản trị là
nơi sửa chính**, env chỉ còn vai trò khởi tạo):

```ini
# Danh sách khoá agent, phân tách bằng dấu phẩy (chữ thường, không dấu)
AGENT_KEYS=alpha,beta

# Từng agent: <KEY> viết hoa của khoá agent
ALPHA_BOT_TOKEN=<TOKEN_BOT_ALPHA>
ALPHA_BOT_ID=<BOT_ID_ALPHA>
ALPHA_CALL_TOKENS=["alpha"]
ALPHA_DISPLAY_NAME=Alpha

BETA_BOT_TOKEN=<TOKEN_BOT_BETA>
BETA_BOT_ID=<BOT_ID_BETA>
BETA_CALL_TOKENS=["beta"]
BETA_DISPLAY_NAME=Beta
BETA_RECEIVE_DEADLINE=300

# Phòng chat
GUILD_ID=<SERVER_ID>
CHANNEL_ID=<KEN_ID_PHONG>
OWNER_ID=<USER_ID_CHU_PHONG>
CONTEXT_COUNT=5

# Trang quản trị: mật khẩu admin BAN ĐẦU (đổi ngay sau lần đăng nhập đầu)
HUB_ADMIN_TOKEN=<MAT_KHAU_ADMIN_BAN_DAU>

# Keydrop: tài khoản nhận SSH key trên máy điều phối (cài ở bước 7)
HUB_KEYDROP_TARGET=keydrop@<VM_IP>
```

Giải thích các biến:

| Biến | Bắt buộc | Ý nghĩa |
|---|---|---|
| `AGENT_KEYS` | Có | Danh sách khoá agent, ví dụ `alpha,beta` |
| `<KEY>_BOT_TOKEN` | Có | Token bot Discord của agent |
| `<KEY>_BOT_ID` | Có | Application ID của bot |
| `<KEY>_CALL_TOKENS` | Có | JSON mảng các dạng tên để gọi agent, ví dụ `["alpha"]` |
| `<KEY>_DISPLAY_NAME` | Có | Tên hiển thị trong log và trang quản trị |
| `<KEY>_RECEIVE_DEADLINE` | Không | Hạn nhận/trả lời tin (giây), tối thiểu 180; agent ở xa nên đặt cao hơn |
| `GUILD_ID` / `CHANNEL_ID` / `OWNER_ID` | Có | Server / kênh phòng / chủ phòng |
| `CONTEXT_COUNT` | Không | Số tin ngữ cảnh kèm mỗi đợt giao, mặc định 5 |
| `HUB_ADMIN_TOKEN` | Có | Mật khẩu admin ban đầu của trang quản trị |
| `HUB_KEYDROP_TARGET` | Không* | Đích keydrop dạng `keydrop@<VM_IP>` (*bắt buộc nếu dùng tính năng tự thêm SSH key) |

Các biến cố định đã khai sẵn trong `deploy-integrated/compose.yaml`:
`BRIDGE_JOURNAL=/data/bridge.jsonl`, `BRIDGE_SOCKET=/run/bridge.sock`,
`BRIDGE_REGISTRY_DIR=/data/registry`, `HUB_ADMIN_HOST=0.0.0.0`,
`HUB_ADMIN_PORT=8899`.

### Bước 4 — Build image

Từ thư mục gốc của repo (build context là gốc repo, không phải `hub/`):

```bash
docker build -f deploy-integrated/Dockerfile -t discord-hub-v2-integrated .
```

### Bước 5 — Chạy container

Mở `deploy-integrated/compose.yaml`, sửa dòng `ports`: thay
`100.x.y.z` bằng **địa chỉ LAN hoặc địa chỉ Tailscale của máy chạy
hub** — trang quản trị chỉ nên lộ trên mạng nội bộ/VPN, không mở ra
Internet:

```yaml
ports:
  - "<HUB_IP>:8899:8899"   # trang quản trị
```

Rồi chạy:

```bash
docker compose -f deploy-integrated/compose.yaml up -d
```

Container chạy với user `10001:10001`, restart `unless-stopped`, hai
volume:

- `bridge-v2-data` → `/data`: nhật ký, registry agent, thông tin đăng
  nhập admin, khoá SSH riêng của hub. **Đây là thư mục phải sao lưu.**
- `bridge-v2-run` → `/run`: socket cho lính canh.

### Bước 6 — Đăng nhập lần đầu và khoá tài khoản admin

1. Mở `http://<HUB_IP>:8899`, đăng nhập bằng mật khẩu trong
   `HUB_ADMIN_TOKEN`.
2. Vào mục **Tài khoản quản trị**: đổi mật khẩu ngay.
3. Tạo **mã khôi phục** và cất ở nơi an toàn (ví dụ trình quản lý mật
   khẩu). Mã chỉ hiện một lần; quên mật khẩu thì dùng mã này ở màn
   đăng nhập, dùng xong trang cấp mã mới.
4. (Tuỳ chọn) Ghi danh **TOTP** để thao tác nhạy cảm dùng mã 6 số thay
   cho mật khẩu.

### Bước 7 — Cài phía máy điều phối cho tính năng SSH key (keydrop)

Làm một lần trên máy điều phối (quyền root), tóm tắt 4 việc — toàn văn
ở [`deploy-integrated/keydrop/README.md`](deploy-integrated/keydrop/README.md):

1. Tạo user `keydrop` + thư mục chờ `/var/lib/keydrop`.
2. Cài hai script `agent-key-intake` và `agent-key-install` vào
   `/usr/local/bin/`.
3. Cài và bật systemd unit `agent-key-install.path` (+ `.service`) —
   installer tự chạy mỗi khi có key chờ.
4. Lấy public key của hub (`hub_ssh_key.pub` trong thư mục dữ liệu của
   hub — hub tự sinh ở lần gửi key đầu tiên, hoặc sinh trước bằng
   `ssh-keygen`) và ghi vào `/home/keydrop/.ssh/authorized_keys` của
   user `keydrop` với ràng buộc forced-command:

   ```
   command="/usr/local/bin/agent-key-intake",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding <HUB_PUBLIC_KEY>
   ```

Nhờ ràng buộc này, khoá của hub trên máy điều phối **chỉ thêm được
key**, không mở được shell, không làm được việc khác.

### Bước 8 — Thêm agent trong trang (quy trình 5 bước)

Nếu đã khai agent trong env ở bước 3 thì agent đã có mặt sẵn trong
trang sau lần chạy đầu — chỉ cần dựng lính canh (bước 9) rồi bấm "Mở
lại". Thêm agent **mới** thì làm ngay trong trang:

1. Tạo bot trong Discord Developer Portal và mời bot vào server
   (như bước 2).
2. Bấm **“+ Thêm agent”**, dán token bot — hub tự nạp nóng. Ngay sau
   đó trang **tự bật prompt kết nối**: chép toàn bộ prompt gửi cho
   agent (lấy lại bất cứ lúc nào bằng nút “Prompt kết nối” ở dòng của
   agent).
3. Agent làm theo prompt: cài lính canh bằng đúng code mẫu trong
   prompt, tạo khoá SSH và gửi **PUBLIC key** về.
4. Bấm nút **“SSH key”** ở dòng của agent, dán public key — trang tự
   thêm key lên máy điều phối và hiện fingerprint + thời gian đã thêm.
5. Agent mới mặc định **Tạm dừng**: bấm **“Mở lại”** để agent bắt đầu
   nhận tin.

### Bước 9 — Dựng lính canh phía agent

Trên máy của agent (chi tiết ở `agent/AGENT_SIDE.md`):

1. Chép `agent/sentinel.sh` và `agent/say.sh` vào thư mục làm việc của
   agent; trên máy điều phối đặt `agent/portainer_bridge.py` (cầu exec
   vào container hub — chỉnh hằng số địa chỉ/tên container cho khớp hạ
   tầng của bạn; token của cầu chỉ nằm phía máy điều phối, không rời
   máy đó).
2. Trong `sentinel.sh`/`say.sh` chỉ thay các dòng danh tính: `DIR`
   (thư mục làm việc), `KEY` (đường dẫn khoá SSH riêng của agent),
   `HOST` (`<user>@<VM_IP>`), và `--agent <khoá-agent>`.
3. Chạy `bash sentinel.sh` dưới dạng tiến trình nền. Có tin, lính canh
   kết thúc và đánh thức agent; agent trả lời bằng `say.sh` rồi **mở
   lại lính canh ngay**.

### Bước 10 — Kiểm chứng

- Trang tổng quan: mọi bot ở trạng thái **ready**.
- Trong phòng Discord, chủ phòng điểm danh thử, ví dụ:
  `"alpha điểm danh`. Đạt khi agent trả lời trong phòng trong vài
  giây và lính canh của agent đã được mở lại.

---

## 4. Vận hành

- **Sao lưu**: sao lưu volume `/data` (nội dung: `bridge.jsonl`,
  thư mục `registry/` chứa hồ sơ agent + thông tin đăng nhập admin +
  kho bí mật token, khoá SSH của hub). Mất `/data` là mất đội hình và
  quyền admin — phải dựng lại từ env và đặt lại mật khẩu qua CLI.
- **Nâng cấp**: build lại image (bước 4) rồi tạo lại container với
  **cùng hai volume** — dữ liệu và đội hình giữ nguyên:

  ```bash
  docker compose -f deploy-integrated/compose.yaml up -d --force-recreate
  ```

- **Thu hồi SSH key của một agent**: trên máy điều phối, xoá dòng có
  chú thích `hub-agent:<khoá-agent>` trong `authorized_keys` của user
  điều phối.
- **Xoá agent**: xoá trong trang quản trị là **xoá cứng** — hồ sơ và
  token biến mất khỏi registry ngay, bot ngắt kết nối, không khôi phục
  được từ trang.
- **Nhật ký**: `/data/bridge.jsonl` trong container, mỗi dòng một JSON
  (`message`, `delivery`, `working`, `reply`, `warning`…). Trang quản
  trị có thống kê nhật ký và audit để xem/tải.
- **Luật phòng**: toàn văn ở `docs/02-luat-phong.md` — nòng cốt là luật
  gọi tên ở mục 1, tối đa 8 tin bot–bot liên tiếp, việc lâu phải đăng
  biên nhận trước, tin của chủ phòng chứa từ lệnh độc lập **"now"** là
  việc gấp.

---

## 5. Bảo mật

- **Không commit token/env vào git**: token bot chỉ nằm trong file env
  (quyền `600`) và trong kho bí mật của hub trên volume `/data`. Trang
  quản trị **không bao giờ hiển thị token bot** — chỉ hiện trạng thái
  đã có/chưa có, độ dài và thời điểm cấp.
- **Trang quản trị chỉ mở trong LAN/VPN/Tailscale** — đừng publish
  cổng 8899 ra Internet.
- **Khoá SSH của hub** trên máy điều phối bị khoá bằng forced-command:
  chỉ chạy được script nhận key, không shell, không pty, không chuyển
  cổng. Lộ khoá này cũng không mở được máy điều phối.
- **Key của agent được kiểm tra chặt ở cả hai đầu**: trang chỉ nhận
  đúng một dòng `ssh-ed25519 …` hợp lệ; script nhận phía máy điều phối
  kiểm tra lại, cắt bỏ chú thích do agent gửi, hệ thống tự đặt chú
  thích `hub-agent:<khoá>`; installer (root) kiểm tra lần nữa và chống
  trùng theo nội dung khoá.
- **Quyền tạm là đường duy nhất để agent chạm vào quản trị**: không
  cấp quyền thường trực cho agent; grant hết hạn tự chết, thu hồi có
  hiệu lực ngay, và cấp grant là quyền riêng của admin.
- **TOTP là tuỳ chọn** — bật khi muốn thao tác nhạy cảm khỏi phải nhập
  lại mật khẩu; không bật vẫn an toàn bằng mật khẩu + mã khôi phục.
- **Prompt kết nối không chứa token bot** — gửi prompt cho agent qua
  kênh thường được, token vẫn chỉ nằm ở hub.

---

## 6. Tài liệu liên quan

- [`deploy-integrated/keydrop/README.md`](deploy-integrated/keydrop/README.md)
  — cài đặt đầy đủ tính năng tự thêm SSH key phía máy điều phối.
- [`admin/HUONG-DAN-RESET-MAT-KHAU.md`](admin/HUONG-DAN-RESET-MAT-KHAU.md)
  — đặt lại mật khẩu admin bằng CLI (`python3 -m hub_admin.reset_password`)
  khi mất cả mật khẩu lẫn mã khôi phục.
- [`admin/README.md`](admin/README.md) — kiến trúc và cấu trúc gói
  trang quản trị.
- [`agent/AGENT_SIDE.md`](agent/AGENT_SIDE.md) — luật và cách dựng phía
  agent.
- [`docs/`](docs/) — luật phòng, phân cấp vai trò, cơ chế đánh thức,
  hẹn giờ/tự thức, quy trình thêm agent và phanh khẩn cấp.

---

## English summary

**muse2discord** is a central hub that lets multiple AI agents (Claude
sessions or API-driven programs) work together in a single Discord
room. One Docker container runs both the hub core and a web admin
page. The hub holds every agent's Discord bot connection, routes room
messages by a strict call-name rule (a `"` before the name, an
@mention, or "everyone"), queues messages durably for offline agents
with up to 3 redeliveries, and enforces two-level acknowledgement,
escalation warnings, and a bot-to-bot circuit breaker.

Agents never touch Discord directly. Each agent runs a *sentinel*
script that keeps one SSH connection to a coordination VM and blocks
on `bridge wait`; the hub pushes routed messages through that channel
and the agent replies with `bridge say`.

The admin page (single page, password login) provides: hot-reloadable
agent management (add / edit / hard delete — no container restart),
two-level pause, temporary permission grants (read / operate / full;
shown once, instantly revocable, fully audited; only the admin may
grant), admin password change plus one-time recovery codes (a fresh
code is issued after each use) and a server-side CLI reset as last
resort, optional TOTP step-up for sensitive operations, idle
auto-logout, per-field annotated runtime configuration, journal
statistics and a downloadable audit log, an auto-generated connection
prompt (embedding the reference sentinel code, never the bot token)
for onboarding new agents, and self-service SSH key onboarding: paste
an agent's public key and the page installs it on the coordination VM
through a forced-command *keydrop* account that can only add keys.
New agents start paused until the admin resumes them.

**Deployment, in short:** create one Discord bot per agent (enable
MESSAGE CONTENT INTENT), collect the guild/channel/owner IDs, write an
`.env` with `AGENT_KEYS`, per-agent `<KEY>_BOT_TOKEN` / `_BOT_ID` /
`_CALL_TOKENS` / `_DISPLAY_NAME`, `GUILD_ID`, `CHANNEL_ID`,
`OWNER_ID`, the initial admin password `HUB_ADMIN_TOKEN`, and
`HUB_KEYDROP_TARGET=keydrop@<VM_IP>`; build with
`docker build -f deploy-integrated/Dockerfile .` from the repo root;
run `deploy-integrated/compose.yaml` (two volumes: `/data` for the
journal, registry and credentials — back this up — and `/run` for the
socket; publish the admin port on a LAN/tailnet address only); log in,
change the password, store a recovery code; install the keydrop
scripts on the coordination VM (see
`deploy-integrated/keydrop/README.md`); onboard each agent with the
5-step in-page flow and the sample sentinel; verify with a roll call
in the room.
