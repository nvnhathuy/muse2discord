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
máy chủ chạy hub, exec vào container hub và treo lệnh `bridge wait` — hub
đẩy tin tới qua đường đó khi có tin được định tuyến cho agent. Agent trả
lời bằng `bridge say`. Nhờ vậy agent không cần token Discord, không cần
quyền truy cập máy chủ chat, và hub kiểm soát toàn bộ việc ai nhận tin gì.
Riêng agent loại **API** không có máy riêng: hub tự thực thi thay chúng
ngay trong container (xem "Bộ thực thi agent API" ở mục 1) — chúng không
có lính canh, không có SSH key, không có đường nào khác ngoài hub.

Kiến trúc tối thiểu chỉ cần **một máy chủ Linux** chạy Docker và có SSH:
hub chạy trên máy đó, lính canh SSH thẳng vào máy đó. Trường hợp hub chạy
trên thiết bị quản qua Portainer (ví dụ NAS) không cho agent SSH trực
tiếp, mới cần tách thêm một máy điều phối làm cửa SSH — xem mục 2.

```
   Phòng Discord  ⇄   ┌─────────────────────────────────────────────┐
   (nhiều bot,        │        MÁY CHỦ LINUX (một máy)              │
    mỗi bot =         │  ┌───────────────────────────────────────┐  │
    một agent)        │  │      CONTAINER HUB (1 cái)            │  │
                      │  │  Lõi hub (bridgehub) ⇄ Trang quản trị │  │
                      │  │  - giữ kết nối mọi bot                │  │
                      │  │  - định tuyến theo tên                │  │
                      │  │  - hàng đợi + nhật ký                 │  │
                      │  │  - registry agent (sửa nóng)          │  │
                      │  └──────────────────▲────────────────────┘  │
                      │        SSH + docker exec vào container      │
                      └─────────────────────▲───────────────────────┘
                                            │ SSH (LAN hoặc Tailscale)
                               ┌────────────┴────────────┐
                               │  Máy của từng agent     │
                               │  lính canh: bridge wait │
                               │  trả lời:   bridge say  │
                               └─────────────────────────┘
```

Biến thể tách hai máy (chỉ khi hub nằm trên thiết bị quản qua Portainer,
không cho SSH trực tiếp): thêm một máy điều phối làm cửa SSH duy nhất —
agent SSH vào máy điều phối, trên đó đặt cầu `agent/portainer_bridge.py`
để exec vào container hub qua Portainer API. Mọi phần dưới đây mặc định
theo bản một máy; chỗ nào khác khi tách hai máy sẽ ghi rõ.

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
  lại khi agent nối lại; mỗi tin chưa chốt được giao lại tối đa 3 lần
  theo lịch cố định 1, 5 và 15 phút kể từ lần giao đầu.
- **Xác nhận 2 mức**: `bridge working` = đã nhận (gia hạn thời gian xử
  lý), `bridge say`/`ack` = đã chốt. Quá 5 phút chưa xong hub cảnh báo
  trong phòng.
- **Phát hiện agent điếc + leo thang theo từng agent**: hub ghi nhịp gọi
  `wait` của từng agent; quá 5 phút không thấy `wait` mà vẫn còn tin chờ
  thì agent bị xếp trạng thái *điếc* (không có tin chờ thì chỉ ghi nhận
  *im* vào file metrics). Điếc đủ 15 phút, hub đăng đúng một tin nhắc
  chủ phòng cho riêng agent đó; agent hồi phục thì hub báo đã hồi và mở
  lại chu kỳ. Trạng thái điếc/im được ghi vào file metrics JSONL để tra
  cứu sau.
- **Ngắt mạch chống lặp**: tối đa 8 tin bot–bot liên tiếp, tránh hai agent
  tự nói chuyện vô hạn.
- **Ngữ cảnh kèm theo**: mỗi đợt giao tin kèm vài tin gần nhất trong
  phòng (số lượng cấu hình được) để agent nắm bối cảnh.
- **Bộ thực thi agent API (trong hub)**: agent loại API không cần máy
  riêng hay lính canh — khi một tin được giao cho chúng, hub tự gọi API
  của nhà cung cấp model ngay trong tiến trình hub: báo `working`, đăng
  tin "<tên agent> đã nhận, đang trả lời...", gọi **song song mọi model**
  đã cấu hình (timeout 40 giây/model, 45 giây tổng, chạy ở luồng riêng
  không chặn vòng sự kiện) và chốt bằng đáp án thành công đầu tiên (cắt
  tối đa 1.900 ký tự). Agent đang tạm dừng, chưa có cấu hình hoặc chưa
  có API key thì hub **không thực thi** — tin giữ nguyên trong hàng chờ
  như khi agent vắng mặt. Mọi lỗi của bộ thực thi được bọc kín: nó không
  bao giờ làm chết hub hay các bot khác.

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
- **Mật khẩu admin**: bản cài mới đăng nhập lần đầu bằng mật khẩu mặc
  định `admin123` (hoặc `HUB_ADMIN_TOKEN` nếu có đặt) và trang **bắt
  buộc đổi ngay** mới cho dùng tiếp; sau đó đổi bất cứ lúc nào trong
  trang. Quên mật khẩu thì dùng **mã
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
  canh). Prompt **không chứa token bot**. Prompt cũng dặn agent
  Muse tạo sẵn **lịch cron 5 phút của runtime** — lưới an toàn
  tự dựng lại lính canh khi lính canh chết.
- **Tự thêm SSH key cho agent (keydrop)**: agent gửi PUBLIC key, admin
  dán vào hộp thoại "SSH key" trong trang — trang tự thêm key lên máy
  chủ (nơi agent SSH tới; nếu tách hai máy thì là máy điều phối) qua một
  kênh SSH bị khoá chặt, hiện fingerprint + thời gian đã thêm. Không
  cần ai đăng nhập máy chủ làm trung gian.
- **Khoá agent trong hub (mặc định)**: agent mới thêm vào luôn bị
  khoá trong hub — đây là quyền cơ bản của agent. SSH key do hub cài
  (qua keydrop) được bọc một cổng bắt buộc (`agent-bridge-gate`):
  key chỉ chạy được lệnh cầu (wait/say/ack/working/laws) với đúng
  khoá agent của nó, không mở được shell, không đi lung tung trên
  máy điều phối. Chủ hub mở khoá cho từng agent bằng nút Khoá/Mở
  khoá trong trang (việc nhạy cảm, có ghi nhật ký) — trang tự rào
  lại key trên máy qua kênh keydrop. Quyền operator trên máy chủ
  (VM) là việc riêng của chủ máy, nằm ngoài hub: chủ cấp/thu hồi
  trực tiếp trên máy, hub không quản.
- **Luật cứng (trang Luật cứng)**: luật phòng chính thức lưu ngay trên
  server của hub (`laws.json` trong thư mục dữ liệu), có **số phiên
  bản** tăng thêm 1 mỗi lần thêm/sửa/xoá. Admin — hoặc agent giữ quyền
  tạm mức **full** — thêm/sửa/xoá được trong trang; quyền tạm mức khác
  chỉ xem. Sửa luật xong, admin chỉ cần đăng vào phòng đúng một câu
  thông báo mẫu (trang có sẵn nút **Prompt thông báo luật** để chép):
  “tất cả agent vào đọc luật cứng trong file laws.json trên hub
  (đường dẫn /data/registry/laws.json) bằng lệnh bridge laws, cập
  nhật và ghi vào bộ nhớ dài hạn” — agent nghe thông báo tự chạy
  lệnh **`bridge laws`** đọc bản mới nhất từ server và ghi vào bộ
  nhớ dài hạn của mình kèm số phiên
  bản. Prompt kết nối sinh khi thêm agent cũng phổ biến sẵn cơ chế
  này cho agent mới.
- **Agent mới mặc định ở trạng thái Tạm dừng** — chỉ nhận tin sau khi
  admin bấm "Mở lại".
- **Agent API (thêm ngay trong trang)**: khi tạo agent, chọn loại
  **API** thay cho SSH — form hiện thêm nhà cung cấp (Gemini / OpenAI),
  ô dán **API key** (ô mật khẩu, không hiện lại) và danh sách model
  (để trống thì dùng danh sách mặc định của nhà cung cấp; riêng Gemini
  có sẵn 4 model đua nhau). Cấu hình API lưu ở
  `registry/api_agents.json`, key lưu ở file riêng
  `registry/api-keys/<khoá-agent>.key`, cả hai đều quyền `600` và nạp
  nóng không cần khởi động lại. **Key không bao giờ được trả ra
  ngoài**: trang, API và audit chỉ hiện trạng thái đã có/chưa có key.
  Đặt cấu hình, đặt/đổi key là **thao tác nhạy cảm** (admin hoặc quyền
  tạm mức full, kèm xác thực lại mật khẩu/TOTP; mức operate bị từ
  chối). Tạo agent API mà thiếu key thì trang từ chối và không để lại
  hồ sơ nửa vời; xoá agent API là xoá luôn cấu hình và file key. Agent
  API không có prompt kết nối cũng không có nút SSH key — chúng chạy
  ngay trong hub, điều kiện duy nhất là bot Discord đúng token đã khai
  trong file env.

### Phía agent

- `agent/sentinel.sh`: lính canh mẫu — một kết nối SSH duy nhất tới máy
  chủ, vòng `bridge wait` qua `docker exec` vào container hub (khi tách
  hai máy: SSH tới máy điều phối, exec qua cầu `portainer_bridge.py`);
  có tin là đánh thức agent, agent xử lý xong mở lại lính canh ngay.
  Lính canh **không bao giờ tự chết hẳn**: lỗi liên tiếp chỉ làm tăng
  thời gian chờ giữa các lần thử (15 → 30 → 60 giây, trần 60), và mỗi
  vòng chờ bị **cắt cứng ở 75 giây** — một kết nối treo không giữ chân
  lính canh vô hạn được; lượt bị cắt có ghi mốc thời gian vào nhật ký
  riêng (`~/sentinel-cuts.log`).
- Agent loại API **không có phía agent**: không máy riêng, không lính
  canh, không SSH key — toàn bộ vòng đời của chúng nằm trong hub.
- `agent/say.sh`: mẫu lệnh trả lời (`bridge say --reply-to ... --final`).
- `agent/AGENT_SIDE.md`: luật ứng xử phía agent (luật gọi tên, kỷ luật
  trả lời, chống trùng).

---

## 2. Chuẩn bị

Yêu cầu chính — **một máy chủ Linux chạy được Docker và có SSH**
(Docker Engine + Compose v2, có sshd): hub chạy trên máy đó, lính canh
của agent SSH thẳng vào máy đó rồi `docker exec` vào container hub.
Không cần máy thứ hai.

Bạn cần:

1. **Một máy chủ Linux** như trên — đây là thành phần duy nhất bắt
   buộc phía máy chủ.
2. **Mạng**: máy của từng agent phải SSH được tới máy chủ — LAN chỉ
   đủ khi mọi agent ở cùng mạng với máy chủ; có agent ở ngoài LAN thì
   bắt buộc có VPN theo tiểu mục *Điều kiện mạng — Tailscale* ngay
   dưới đây.
3. **Tài khoản Discord** và một server (guild) có một kênh phòng cho
   đội agent.
4. **Một ứng dụng bot Discord cho mỗi agent** (xem bước 2 khi triển
   khai).

### Điều kiện mạng — Tailscale

- **Mọi agent ở cùng mạng LAN với máy chủ:** không cần gì thêm — dùng
  địa chỉ LAN của máy chủ cho mọi kết nối.
- **Có BẤT KỲ agent nào ở ngoài mạng LAN** (máy đặt ở nơi khác, VPS,
  nhà riêng của thành viên...): **bắt buộc** có **Tailscale** (hoặc
  một VPN tương đương). Cài Tailscale trên máy chủ — và trên máy điều
  phối nếu dùng bản tách hai máy — cùng trên máy của từng agent ở xa;
  tất cả phải ở cùng một tailnet và đã đăng nhập/duyệt máy xong.
- **Khi đi qua Tailscale**, địa chỉ dùng cho lính canh SSH vào và cho
  biến `HUB_KEYDROP_TARGET` là **địa chỉ Tailscale của máy đích**
  (IP dạng `100.x.y.z` hoặc tên MagicDNS) — **không** dùng IP LAN.
- **Không** mở cổng SSH của máy chủ ra Internet để thay cho VPN.
- Trước khi triển khai tiếp, kiểm tra `ssh <user>@<địa chỉ đích>`
  (LAN hoặc Tailscale tuỳ trường hợp) thông từ máy của agent.

**Trường hợp tách hai máy (tuỳ chọn):** nếu hub chạy trên thiết bị quản
qua Portainer (ví dụ NAS) không cho agent SSH trực tiếp vào thiết bị
đó, hãy đặt thêm **một máy điều phối** làm cửa SSH duy nhất: agent SSH
vào máy điều phối, trên máy điều phối đặt cầu `agent/portainer_bridge.py`
exec vào container hub qua Portainer API; tính năng keydrop (bước 7)
cũng cài trên máy điều phối thay vì trên thiết bị chạy hub. Ngoài khác
biệt đó, mọi bước triển khai đều như bản một máy.

### Loại agent nào thêm được — phân loại 1A / 1B

<details>
<summary>Bấm để mở nội dung</summary>

Thêm được hay không phụ thuộc vào **loại agent** — hãy phân loại
trước, trước khi làm bất cứ bước triển khai nào khác. Phần này
chỉ phân loại theo bản chất của agent; các yêu cầu vận hành sau
khi thêm nằm ở tiểu mục *Yêu cầu sau khi thêm để agent hoạt
động* ngay bên dưới.

**1A — Thêm được vào hệ thống và hoạt động tốt:**

1. **Agent có máy tính riêng, không dùng API, nhưng đánh thức
   được** — ví dụ **Muse**: chạy Muse trên máy riêng của
   agent, hub có tin là đánh thức agent dậy xử lý được qua đường
   lính canh.
2. **Agent có API** — có **API key** của hãng model (ví dụ
   Gemini, OpenAI) do chính bạn tạo; một runner gọi API để trả
   lời thay cho agent. Hệ thống không có sẵn key nào.

**1B — Không thêm được, hoặc hoạt động không ổn định — không
nên thêm:**

1. **Agent dạng tài khoản web** (chat trên trình duyệt): không
   đánh thức được, hub không gọi tới được — **không thêm
   được**.
2. **Agent không có máy riêng, cũng không có API:** không có
   đường nào để hub đánh thức hay gọi API — **không thêm
   được**.
3. Agent thuộc diện 1A nhưng máy riêng **hay tắt/ngủ**: hub chỉ
   xếp hàng tin chờ, agent trả lời chập chờn — **không ổn
   định**.

</details>

### Yêu cầu sau khi thêm để agent hoạt động

<details>
<summary>Bấm để mở nội dung</summary>

Chỉ agent thuộc diện **1A** mới thêm được. Sau khi thêm, các yêu
cầu dưới đây quyết định agent có hoạt động được hay không —
thiếu một yêu cầu là agent im lặng và hub không tự bù được.

**Yêu cầu chung:**

1. Mỗi agent có **một bot Discord riêng**; bot đã được mời vào
   server, đã bật **MESSAGE CONTENT INTENT** trong Discord
   Developer Portal, và có quyền **xem kênh + gửi tin** trong
   kênh phòng — thiếu là hub không thấy tin của phòng.
2. Hub đang chạy và bạn vào được trang quản trị.
3. Đường mạng từ máy của agent (hoặc máy chạy runner) tới máy
   chủ hub thông suốt — cùng LAN hoặc cùng tailnet Tailscale
   (xem tiểu mục *Điều kiện mạng — Tailscale* ở trên).

**Riêng loại Muse (1A.1):**

- Cài lính canh bằng **nguyên code mẫu lính canh** trong prompt
  kết nối, không tự viết lại.
- Thêm **public key SSH** của agent qua trang quản trị (nút
  “SSH key”) — private key ở lại máy của agent.
- Máy của agent **chạy thường trực**, không tắt/ngủ trong thời
  gian trực, trên đó có một **phiên Muse đang sống** để lính
  canh đánh thức khi có tin — lính canh không khởi động hộ
  phiên đã chết.

**Riêng loại API (1A.2):**

- Một **runner chạy thường trực** trên máy chủ (hoặc máy điều
  phối nếu tách hai máy): nhận tin từ hub rồi gọi API.
- **API key còn quota/số dư** — hết quota/hết tiền là agent im
  lặng, không trả lời.
- Chi phí tính theo lượt trả lời, tuỳ model bạn chọn.

Không cần: cơ sở dữ liệu riêng, Redis, hay dịch vụ ngoài nào khác —
toàn bộ trạng thái nằm trong thư mục dữ liệu `/data` của container.

</details>

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

Mỗi agent cần **một ứng dụng bot Discord riêng**. Cách tạo bot,
bật intent, lấy token và mời bot vào server nằm trọn ở mục
**“Tạo bot Discord và thêm agent — chi tiết từng bước”** bên
dưới (phần A và B) — làm theo mục đó; ở bước này chỉ thu thập
các ID cần cho file env.

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

# Trang quản trị: mật khẩu admin ban đầu — KHÔNG bắt buộc.
# Bỏ trống thì mật khẩu mặc định là admin123; dù đặt hay không, lần
# đăng nhập đầu tiên trang đều bắt buộc đổi mật khẩu mới dùng tiếp.
# HUB_ADMIN_TOKEN=<MAT_KHAU_ADMIN_BAN_DAU>

# Keydrop: tài khoản nhận SSH key trên máy chủ (cài ở bước 7;
# nếu tách hai máy thì là máy điều phối)
HUB_KEYDROP_TARGET=keydrop@<HOST_IP>
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
| `HUB_ADMIN_TOKEN` | Không | Mật khẩu admin ban đầu của trang quản trị — chỉ đặt khi muốn mật khẩu đầu khác mặc định `admin123`; dù đặt hay không, lần đăng nhập đầu đều bắt buộc đổi mật khẩu |
| `HUB_KEYDROP_TARGET` | Không* | Đích keydrop dạng `keydrop@<HOST_IP>` — máy chủ, hoặc máy điều phối nếu tách hai máy (*bắt buộc nếu dùng tính năng tự thêm SSH key) |

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

1. Mở `http://<HUB_IP>:8899`, đăng nhập với tên đăng nhập `admin` và
   mật khẩu **`admin123`** (hoặc giá trị `HUB_ADMIN_TOKEN` nếu bạn đã
   đặt ở bước 3).
2. Trang **bắt buộc đổi mật khẩu ngay** ở lần đăng nhập đầu: nhập mật
   khẩu mới (ít nhất 8 ký tự, không được trùng mật khẩu mặc định) —
   chưa đổi xong thì chưa vào được trang chính, mọi thao tác khác đều
   bị khoá.
3. Đổi xong, trang hiện **mã khôi phục một lần** — cất ngay ở nơi an
   toàn (ví dụ trình quản lý mật khẩu). Quên mật khẩu thì dùng mã này
   ở màn đăng nhập để đặt lại, dùng xong trang cấp mã mới.
4. (Tuỳ chọn) Ghi danh **TOTP** để thao tác nhạy cảm dùng mã 6 số thay
   cho mật khẩu.

### Bước 7 — Cài phía máy chủ cho tính năng SSH key (keydrop)

Làm một lần trên máy chủ (quyền root; nếu tách hai máy thì làm trên
máy điều phối), tóm tắt 4 việc — toàn văn
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

Nhờ ràng buộc này, khoá của hub trên máy chủ **chỉ thêm được
key**, không mở được shell, không làm được việc khác.

### Bước 8 — Thêm agent trong trang

Nếu đã khai agent trong env ở bước 3 thì agent đã có mặt sẵn trong
trang sau lần chạy đầu — chỉ cần dựng lính canh (bước 9) rồi bấm "Mở
lại". Thêm agent **mới** thì làm ngay trong trang theo mục **“Tạo bot
Discord và thêm agent — chi tiết từng bước”** ngay dưới đây (phần A–E:
tạo bot, mời bot, điền form, mở agent, kết nối agent).

**Trước khi thêm, đối chiếu phân loại ở tiểu mục “Loại agent
nào thêm được — phân loại 1A / 1B” (mục 2. Chuẩn bị)** — nhất
là: agent dạng tài khoản web không thêm được.

### Tạo bot Discord và thêm agent — chi tiết từng bước

Bản chi tiết của bước 2 (tạo bot) và bước 8 (thêm agent trong
trang) — làm lần lượt, không bỏ bước.

<details>
<summary>Bấm để mở hướng dẫn chi tiết</summary>

**A. Tạo ứng dụng và bot trong Discord Developer Portal**

1. Mở đúng địa chỉ https://discord.com/developers/applications,
   đăng nhập, bấm **New Application**, đặt tên cho ứng dụng rồi
   bấm Create.
2. Vào mục **Bot** → khối **Privileged Gateway Intents**: bật
   **MESSAGE CONTENT INTENT**. Thiếu intent này bot nhận được
   tin nhưng **không đọc được nội dung tin**, hub coi như không
   có tin.
3. Cũng ở mục Bot, bấm **Reset Token** để lấy token bot. Token
   là bí mật, tương đương mật khẩu của bot:
   - Chỉ dán token vào đúng ô **Token bot** của trang quản trị
     hub — đó là nơi duy nhất token thuộc về.
   - Tuyệt đối không gửi token qua chat, không chụp màn hình có
     token.
   - Lỡ lộ token thì bấm **Reset Token** lại ngay: token cũ
     chết ngay lập tức, dùng token mới vừa tạo.
   - Ghi chú: đoạn đầu của token bot là mã base64 của bot user
     id — bản thân id thì không bí mật, cả chuỗi token mới là
     bí mật.
4. Lấy **bot user id**: mở trang **General Information** của
   ứng dụng — số **Application ID** chính là bot user id. Cách
   khác: trong Discord, bật Developer Mode (Cài đặt → Nâng cao
   → Developer Mode) rồi chép ID của bot trong danh sách thành
   viên của server.
5. Lấy **username bot**: xem ở mục Bot trong Portal.

**B. Mời bot vào server**

1. Vào mục **OAuth2 → URL Generator**, tích scope `bot`.
2. Ở bảng quyền hiện ra, tích đúng các quyền sau: View Channel,
   Send Messages, Read Message History, Add Reactions, Embed
   Links, Attach Files. **Không** tích Administrator, **không**
   tích Manage Server / Manage Channels — bot chỉ cần đọc và
   gửi tin.
3. Chép link được sinh ở cuối trang, mở link, chọn server và
   xác nhận mời.
4. Sau khi bot vào server, kiểm tra quyền theo kênh: trong từng
   kênh bot sẽ làm việc, bot phải có quyền View Channel và Send
   Messages ở kênh đó (quyền kênh ghi đè quyền server; kênh
   riêng tư phải cấp quyền riêng cho bot).

**C. Thêm agent trong trang quản trị hub — từng ô một**

Mở trang quản trị hub → mục Agent → **“+ Thêm agent”**, điền:

- **Agent id**: tên máy của ghế — chỉ chữ thường không dấu, số
  và dấu gạch ngang; không khoảng trắng; không trùng agent
  khác; **không đổi được sau khi tạo**. Id đặt tên cho *cái
  ghế*, không phải tài khoản ngồi vào — sau này đổi tài khoản
  hay model sau lưng ghế thì id vẫn giữ nguyên.
- **Tên hiển thị**: tên người trong phòng nhìn thấy; sửa được
  bất cứ lúc nào.
- **Discord bot user id**: số Application ID lấy ở bước A.4.
- **Username bot**: username lấy ở bước A.5.
- **Các dạng gọi tên**: các tên dùng để gọi agent trong phòng,
  phân tách bằng dấu phẩy; thường đặt trùng agent id.
- **Loại agent**: `SSH` = ghế agent sống có máy riêng, nối hub
  qua cầu (bridge); `API` = agent chạy ngay trong hub bằng API
  key của hãng model (chọn loại này thì điền thêm khối API key
  bên dưới).
- **Nhận tin “tất cả”**: để trống (không tích), trừ khi agent
  bắt buộc phải nghe các lời gọi chung cả phòng.
- **Token bot**: dán token bot ở bước A.3 vào ô này.

**D. Sau khi tạo**

1. Hub tự nạp nóng agent mới — không cần khởi động lại hub hay
   container.
2. Agent mới mặc định ở trạng thái **Tạm dừng**: bấm **“Mở
   lại”** ở dòng của agent thì agent mới bắt đầu nhận tin.
3. Điểm danh thử trong kênh của agent (gọi đúng dạng gọi tên
   đã đặt) và xác nhận agent trả lời được.

**E. Kết nối agent (prompt kết nối và SSH key)**

1. Ngay sau khi thêm, trang **tự bật prompt kết nối**: chép toàn
   bộ prompt gửi cho agent (lấy lại bất cứ lúc nào bằng nút
   “Prompt kết nối” ở dòng của agent).
2. Agent làm theo prompt: cài lính canh bằng đúng code mẫu trong
   prompt, tạo khoá SSH và gửi **PUBLIC key** về.
3. Bấm nút **“SSH key”** ở dòng của agent, dán public key — trang
   tự thêm key lên máy chủ và hiện fingerprint + thời gian đã
   thêm ngay trong hộp thoại.

</details>

### Bước 9 — Dựng lính canh phía agent

Trên máy của agent (chi tiết ở `agent/AGENT_SIDE.md`):

1. Chép `agent/sentinel.sh` và `agent/say.sh` vào thư mục làm việc của
   agent. Bản một máy: lính canh SSH thẳng vào máy chủ rồi
   `docker exec` vào container hub — không cần cầu riêng. Chỉ khi tách
   hai máy (hub quản qua Portainer) mới đặt thêm
   `agent/portainer_bridge.py` trên máy điều phối (cầu exec vào
   container hub qua Portainer API — chỉnh hằng số địa chỉ/tên
   container cho khớp hạ tầng của bạn; token của cầu chỉ nằm phía máy
   điều phối, không rời máy đó).
2. Trong `sentinel.sh`/`say.sh` chỉ thay các dòng danh tính: `DIR`
   (thư mục làm việc), `KEY` (đường dẫn khoá SSH riêng của agent),
   `HOST` (`<user>@<HOST_IP>` — địa chỉ máy chủ, hoặc máy điều phối
   nếu tách hai máy), và `--agent <khoá-agent>`.
3. Chạy `bash sentinel.sh` dưới dạng tiến trình nền. Có tin, lính canh
   kết thúc và đánh thức agent; agent trả lời bằng `say.sh` rồi **mở
   lại lính canh ngay**.
4. Tạo **lịch cron 5 phút của runtime** của agent (job tên
   `<khoá-agent>-sentinel-watchdog`): mỗi lượt kiểm tra lính canh còn
   sống không — chết thì dựng lại — và kiểm tra hàng đợi hub, có tin
   chưa ack mà không lính canh nào đang giữ thì xử lý luôn trong lượt
   đó. Đây là lưới an toàn khi lính canh chết hẳn (runtime khởi động
   lại, tiến trình bị giết): lính canh vẫn là đường nhận tin chính,
   cron chỉ dựng lại nó, nên agent sống lại tối đa khoảng 5 phút sau.
   Prompt kết nối sinh khi thêm agent đã dặn sẵn việc này.

### Bước 10 — Kiểm chứng

- Trang tổng quan: mọi bot ở trạng thái **ready**.
- Trong phòng Discord, chủ phòng điểm danh thử, ví dụ:
  `"alpha điểm danh`. Đạt khi agent trả lời trong phòng trong vài
  giây và lính canh của agent đã được mở lại.

---

## 4. Vận hành

- **Sao lưu**: sao lưu volume `/data` (nội dung: `bridge.jsonl`,
  thư mục `registry/` chứa hồ sơ agent, cấu hình agent API
  (`api_agents.json`) và file key của chúng (`api-keys/`), thông tin
  đăng nhập admin + kho bí mật token, khoá SSH của hub). Mất `/data`
  là mất đội hình và quyền admin — phải dựng lại từ env và đặt lại
  mật khẩu qua CLI.
- **Nâng cấp**: build lại image (bước 4) rồi tạo lại container với
  **cùng hai volume** — dữ liệu và đội hình giữ nguyên:

  ```bash
  docker compose -f deploy-integrated/compose.yaml up -d --force-recreate
  ```

- **Thu hồi SSH key của một agent**: trên máy chủ (máy điều phối nếu
  tách hai máy), xoá dòng có chú thích `hub-agent:<khoá-agent>` trong
  `authorized_keys` của user điều phối.
- **Xoá agent**: xoá trong trang quản trị là **xoá cứng** — hồ sơ và
  token biến mất khỏi registry ngay, bot ngắt kết nối, không khôi phục
  được từ trang.
- **Nhật ký**: `/data/bridge.jsonl` trong container, mỗi dòng một JSON
  (`message`, `delivery`, `working`, `reply`, `warning`…). Trang quản
  trị có thống kê nhật ký và audit để xem/tải.
- **Luật phòng**: toàn văn ở `docs/02-luat-phong.md` — nòng cốt là luật
  gọi tên ở mục 1, tối đa 8 tin bot–bot liên tiếp, việc lâu phải đăng
  biên nhận trước, tin của chủ phòng chứa từ lệnh độc lập **"now"** là
  việc gấp. Luật cứng áp dụng ngay trong phòng thì quản lý ở **trang
  Luật cứng** của trang quản trị (xem mục Chức năng): luật lưu trên
  server của hub, agent tự đọc bản mới nhất bằng `bridge laws` sau
  câu thông báo mẫu.

---

## 5. Bảo mật

- **Không commit token/env vào git**: token bot chỉ nằm trong file env
  (quyền `600`) và trong kho bí mật của hub trên volume `/data`. Trang
  quản trị **không bao giờ hiển thị token bot** — chỉ hiện trạng thái
  đã có/chưa có, độ dài và thời điểm cấp.
- **Trang quản trị chỉ mở trong LAN/VPN/Tailscale** — đừng publish
  cổng 8899 ra Internet.
- **Khoá SSH của hub** trên máy chủ bị khoá bằng forced-command:
  chỉ chạy được script nhận key, không shell, không pty, không chuyển
  cổng. Lộ khoá này cũng không mở được máy chủ.
- **Key của agent được kiểm tra chặt ở cả hai đầu**: trang chỉ nhận
  đúng một dòng `ssh-ed25519 …` hợp lệ; script nhận phía máy chủ
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
- **Agent SSH bị rào bằng cổng bắt buộc (agent-bridge-gate)**: key do
  hub cài cho agent chỉ chạy được đúng ba thứ — (1) vòng lặp lính canh
  **khớp nguyên văn** bản chuẩn duy nhất, (2) các lệnh cầu một lần
  (`wait`/`say`/`ack`/`working`/`laws`) mang đúng khoá agent của
  chính nó, (3) kênh file `hub-files`: thư mục `shared/` cả đội
  đọc/ghi và thư mục riêng mang tên agent (chỉ chủ nhân vào được);
  đường dẫn bị kiểm tra từng đoạn (cấm `..`, cấm đường tuyệt đối,
  chống symlink thoát ra ngoài), mỗi file tối đa 256KB, ghi theo kiểu
  nguyên tử. Mọi lệnh ngoài bề mặt này bị **từ chối và ghi vào nhật
  ký cổng**. Agent bị khoá trong hub theo mặc định; mở khoá là thao
  tác nhạy cảm trong trang quản trị, có ghi audit.
- **Agent API bị khoá cứng bằng code — không bằng luật miệng**: (1)
  model **không có bất kỳ bề mặt công cụ nào**: đáp án của model được
  dùng nguyên văn làm tin trả lời trong phòng, kể cả khi đáp án trông
  giống một mệnh lệnh hay một lệnh gọi công cụ; (2) đích gọi ra được
  **ghim cứng trong code** vào đúng host của nhà cung cấp (Gemini /
  OpenAI) — không một URL nào được lấy từ cấu hình hay từ nội dung
  tin nhắn; (3) API key chỉ được gửi trong header tới đúng host của
  nhà cung cấp đó, và **không xuất hiện** trong nhật ký, audit, thông
  báo lỗi hay bất kỳ đầu ra nào của hệ thống; (4) **ngân sách 500
  lượt gọi nhà cung cấp / agent / ngày**, được đếm bằng code *trước*
  mỗi lần gọi và lưu bền qua khởi động lại (`api_budget.json`) — hết
  ngân sách thì bộ thực thi ngừng gọi và đăng tin báo hết lượt vào
  phòng; (5) **luật cứng được tiêm từ file thật mỗi lượt**: toàn văn
  luật hiện hành kèm số phiên bản được đọc từ `laws.json` của hub và
  đưa vào prompt ở mọi lần thực thi — agent API không tự đọc luật
  được thì hub đưa tận nơi, nên chúng không thể nhớ sai luật hay bịa
  ra luật không có thật; (6) prompt bị cắt ở 6.000 ký tự và đáp án ở
  1.900 ký tự, nên một lượt thực thi luôn nằm trong giới hạn của
  Discord và của hub.
- **Thao tác với API key là thao tác nhạy cảm**: đặt cấu hình API,
  đặt/đổi key của agent đòi hỏi phiên admin hoặc quyền tạm mức full
  kèm xác thực lại (mật khẩu hoặc mã TOTP); quyền tạm mức operate bị
  từ chối (403). Audit của các thao tác này chỉ ghi nhà cung cấp và
  số lượng model — không ghi key, không ghi một phần của key.

---

## 6. Tài liệu liên quan

- [`deploy-integrated/keydrop/README.md`](deploy-integrated/keydrop/README.md)
  — cài đặt đầy đủ tính năng tự thêm SSH key phía máy chủ.
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
script that keeps one SSH connection to the hub host and blocks on
`bridge wait` (via `docker exec` into the hub container); the hub
pushes routed messages through that channel and the agent replies
with `bridge say`. By default everything runs on **one Linux host**
with Docker and SSH; only when the hub lives on a Portainer-managed
device (e.g. a NAS) that agents cannot SSH into directly do you add a
separate coordination machine as the single SSH door, running a
Portainer bridge instead of plain `docker exec`.

**Networking condition:** if every agent is on the same LAN as the
host, nothing extra is needed; if **any** agent is outside that LAN,
Tailscale (or an equivalent VPN) is **required** — install it on the
host (and on the coordination machine, if split) and on each remote
agent's machine, all joined to the same tailnet. Over Tailscale, use
the Tailscale IP (`100.x.y.z`) or MagicDNS name — not the LAN IP —
for sentinel SSH connections and for `HUB_KEYDROP_TARGET`, and never
expose the host's SSH port to the Internet instead of using a VPN.

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
an agent's public key and the page installs it on the host through a
forced-command *keydrop* account that can only add keys.
New agents start paused until the admin resumes them.

**Two kinds of agents.** *Live* agents run the reference sentinel on
their own machine (the sentinel never exits permanently — consecutive
errors only back off 15s → 30s → 60s, and each wait turn is hard-capped
at 75 seconds so a hung connection cannot pin it forever). *API*
agents have no machine, no sentinel and no SSH key at all: the hub
itself executes for them. When a message is routed to an API agent,
the hub acknowledges it, posts a "received, answering..." part, races
**all configured models in parallel** (40s per model, 45s overall, off
the event loop) and posts the first successful answer (trimmed to
1,900 characters). API agents are created entirely from the admin
page — kind, provider (Gemini / OpenAI), model list and API key in one
form; configuration lives in `registry/api_agents.json` and each key
in its own `registry/api-keys/<agent>.key` file (mode 0600, hot
reloaded). A suspended API agent, or one missing configuration or a
key, is simply not executed — its messages stay queued.

**Security model, in brief.** Hub-installed SSH keys are *locked by
default* behind a forced-command gate (`agent-bridge-gate`) that
admits exactly three things: the byte-identical reference sentinel
loop, one-shot bridge commands (`wait`/`say`/`ack`/`working`/`laws`)
carrying the agent's own key, and the `hub-files` channel (a shared
directory plus a private per-agent directory, with per-segment path
checks, symlink-escape protection and a 256KB per-file cap). Anything
else is denied and logged. API agents are fenced **in code, not by
policy**: (1) the model has no tool surface whatsoever — its output
is used verbatim as a plain chat reply, even if it looks like a
command; (2) provider hosts are pinned in the source code — no URL
ever comes from configuration or message content; (3) an API key is
sent only in the header to its own provider's host and never appears
in logs, audit records or error messages; (4) a hard budget of **500
provider calls per agent per day** is counted in code *before* every
call and persisted across restarts — when it runs out, execution
stops and the room is told; (5) the current house rules are injected
from the hub's real `laws.json` (full text plus version number) into
every prompt, so an API agent cannot misremember or invent rules;
(6) setting an API configuration or key is a sensitive operation —
admin session or full grant plus step-up re-authentication; operate
grants are refused, and the audit record contains only the provider
and the model count, never the key.

**Deployment, in short:** create one Discord bot per agent (enable
MESSAGE CONTENT INTENT), collect the guild/channel/owner IDs, write an
`.env` with `AGENT_KEYS`, per-agent `<KEY>_BOT_TOKEN` / `_BOT_ID` /
`_CALL_TOKENS` / `_DISPLAY_NAME`, `GUILD_ID`, `CHANNEL_ID`,
`OWNER_ID`, optionally `HUB_ADMIN_TOKEN` (the first admin password —
when unset it defaults to `admin123`, and either way the first login
forces an immediate password change), and
`HUB_KEYDROP_TARGET=keydrop@<HOST_IP>`; build with
`docker build -f deploy-integrated/Dockerfile .` from the repo root;
run `deploy-integrated/compose.yaml` (two volumes: `/data` for the
journal, registry and credentials — back this up — and `/run` for the
socket; publish the admin port on a LAN/tailnet address only); log in
as `admin` with the default `admin123` (or your `HUB_ADMIN_TOKEN`),
complete the forced first-login password change, and store the
recovery code the page shows; install the keydrop
scripts on the host (see
`deploy-integrated/keydrop/README.md`); onboard each agent with the
5-step in-page flow and the sample sentinel; verify with a roll call
in the room.
