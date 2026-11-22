English version: [README.en.md](README.en.md)

# Cầu Discord v2 — hub trung tâm cho phòng agent

Một **hub** duy nhất (tiến trình Python/asyncio) giữ kết nối của nhiều bot Discord — mỗi bot là một agent — route tin nhắn theo luật gọi tên, giữ hàng đợi + nhật ký bền vững, và đánh thức từng agent qua lính canh phía agent. Bản tham chiếu đang chạy thật: phòng `#ai-agent` với 3 agent **Lead – Deputy – Worker**, hub chạy trong container Docker trên một Docker host.

```
Discord ⇄ [hub: bridgehub/] ⇄ socket /run/bridge.sock ⇄ [lính canh phía agent] ⇄ agent
```

Cấu trúc repo:

| Thư mục | Nội dung |
|---|---|
| `hub/` | Code hub: package `bridgehub/`, CLI `bridge`, `tests/`, `Dockerfile`, `compose.yaml` |
| `agent/` | Gói phía agent mẫu: `sentinel.sh`, `say.sh`, `portainer_bridge.py`, `connect_via_proxy.py`, `AGENT_SIDE.md` |
| `deploy/` | Script triển khai qua Portainer: `portainer.py`, `build_image.py`, `create_container.py`, bản mẫu `hub.env.example` |
| `docs/` | Tài liệu vận hành đầy đủ + `SELF_WAKE_PROPOSAL.md` |

---

## Điều kiện cần

1. **Một máy chạy Docker** cho hub — bản tham chiếu chạy trên một Docker host (Docker qua Portainer). Bất kỳ máy Linux nào chạy Docker đều được; nếu không dùng Portainer thì dùng `hub/compose.yaml` thay cho các script trong `deploy/`.
2. **Một ứng dụng bot Discord cho mỗi agent** (3 bot cho 3 agent), mỗi bot đã **bật MESSAGE CONTENT INTENT** trong Discord Developer Portal và đã mời vào server, được cấp quyền ở kênh phòng: View Channel, Read Message History, Send Messages, Add Reactions (không cần Administrator).
3. **Một kênh phòng** trên server đó — nơi chủ phòng ra lệnh và các agent báo cáo.
4. **Tailscale (bắt buộc)** — xem mục riêng ngay dưới đây. Thiếu Tailscale là không dùng được với mô hình agent ở ngoài mạng nhà.
5. **Máy của từng agent** SSH được tới máy chủ chứa hub/Portainer (qua Tailscale), có Python 3 và bash.

### Tailscale — đường truy cập duy nhất vào nhà

Toàn bộ đường truy cập của hệ thống chạy qua **Tailscale**:

- Hub ở trong mạng nhà (trên Docker host); các agent ở **ngoài mạng nhà**. Không có đường nào khác vào: agent vào nhà qua Tailscale trước, rồi mới SSH / gọi Portainer tới hub.
- Máy chủ chạy các script cầu phía máy chủ (hub host) có một **địa chỉ Tailscale riêng** — địa chỉ này chính là đích SSH trong script lính canh (`operator@<TAILSCALE_IP>`).
- Nếu runtime của agent không đi thẳng được tới Tailscale (ví dụ bị chặn bởi proxy của nhà cung cấp), dùng **proxy connector** `agent/connect_via_proxy.py` làm `ProxyCommand` cho SSH — script này relay CONNECT qua proxy của runtime; địa chỉ và thông tin xác thực của proxy đọc từ biến môi trường `HTTPS_PROXY` lúc chạy, không ghi trong file.

**Bước cài Tailscale (làm trước khi dựng agent):**

1. Cài Tailscale trên **máy chủ/VM chứa các script cầu** (nơi agent sẽ SSH tới) và trên **máy của từng agent**.
2. Đăng nhập tất cả vào **cùng một tailnet**.
3. Ghi lại **địa chỉ Tailscale của VM/máy chủ** (dạng `100.x.x.x`, xem bằng `tailscale ip -4`) — địa chỉ này điền vào lính canh của từng agent (biến `HOST`).
4. Kiểm tra từ máy của agent: `ssh <user>@<địa-chỉ-tailscale>` phải vào được **trước** khi chạy lính canh. SSH qua Tailscale chưa thông thì đừng chẩn đoán gì khác.

---

## Bước 1 — Tạo bot và thu thập ID/token

Với mỗi agent, trong [Discord Developer Portal](https://discord.com/developers/applications):

1. New Application → mục **Bot** → Reset Token, lưu token lại (đây là `<AGENT>_BOT_TOKEN`, chỉ hiển thị một lần).
2. Bật **MESSAGE CONTENT INTENT** ở mục Bot → Privileged Gateway Intents.
3. Mục **OAuth2 → URL Generator**: chọn scope `bot`, quyền View Channel / Read Message History / Send Messages / Add Reactions → mở link để mời bot vào server.
4. Ghi lại: **Application ID** của bot (= `<AGENT>_BOT_ID`), **Server ID** (`GUILD_ID` — bật Developer Mode trong Discord, chuột phải vào server → Copy Server ID), **Channel ID** của kênh phòng (`CHANNEL_ID`), và **User ID của chủ phòng** (`OWNER_ID`).

## Bước 2 — Build image hub

Từ thư mục `hub/`:

```sh
docker build -t discord-hub-v2:trial .
```

Hoặc qua Portainer API bằng script có sẵn (bản tham chiếu): đóng gói thư mục `hub/` thành `ctx.tgz` rồi chạy `deploy/build_image.py` — script POST bản build lên Portainer endpoint và in log build.

Kiểm chứng code trước khi build (không cần Discord, không cần token):

```sh
cd hub && python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

## Bước 3 — Tạo `hub.env`

```sh
cp deploy/hub.env.example hub.env
chmod 600 hub.env
```

Điền giá trị thật lấy ở Bước 1:

- `<AGENT>_BOT_TOKEN`, `<AGENT>_BOT_ID` cho từng agent (LEAD / DEPUTY / WORKER).
- `<AGENT>_CALL_TOKENS`: danh sách JSON các dạng tên mà luật gọi tên nhận diện, ví dụ `LEAD_CALL_TOKENS=["lead"]`, `WORKER_CALL_TOKENS=["worker"]`, `DEPUTY_CALL_TOKENS=["deputy"]`.
- `<AGENT>_DISPLAY_NAME`: tên hiển thị trong log.
- `<AGENT>_RECEIVE_DEADLINE`: hạn nhận tin ban đầu (giây), tối thiểu 180 — ví dụ: Lead 180, Deputy 180, Worker 300 (một agent ở xa có thể đặt hạn dài hơn).
- `GUILD_ID`, `CHANNEL_ID`, `OWNER_ID`; `CONTEXT_COUNT` (số tin ngữ cảnh kèm theo mỗi đợt giao, mặc định 5).
- `BRIDGE_SOCKET=/run/bridge.sock` và `BRIDGE_JOURNAL=/data/bridge.jsonl` giữ nguyên như bản mẫu.

## Bước 4 — Tạo container hub

Thông số bản tham chiếu (khớp `hub/compose.yaml`):

- Tên: `discord-hub-v2`, image `discord-hub-v2:trial`.
- User: `10001:10001`.
- Bind 2 volume: `bridge-v2-data:/data` (nhật ký bền vững) và `bridge-v2-run:/run` (socket).
- Restart policy: `unless-stopped`.
- Env: toàn bộ biến trong `hub.env` (compose nạp qua `env_file`; script Portainer `deploy/create_container.py` đọc `hub.env` và truyền thẳng vào cấu hình container).

Chạy bằng compose: `docker compose up -d` trong `hub/` (đặt `BRIDGE_ENV_FILE` trỏ tới `hub.env` nếu file không nằm cạnh compose). Chạy bằng Portainer: lần lượt `deploy/create_container.py` (tạo volume + container) rồi start container và đọc log kiểm chứng.

## Bước 5 — Kiểm chứng hub

```sh
# Trong container:
docker exec discord-hub-v2 sh -c 'BRIDGE_SOCKET=/run/bridge.sock bridge --help'
# Xem nhật ký: 3 bot đăng nhập, hub bắt đầu ghi bridge.jsonl
docker exec discord-hub-v2 sh -c 'tail -n 50 /data/bridge.jsonl'
docker logs discord-hub-v2 --tail 60
```

Đạt khi: container ở trạng thái running, cả 3 bot đều online trong Discord, và gõ thử trong phòng một tin gọi đúng dạng (ví dụ `"lead điểm danh`) thì nhật ký có dòng `message` + `delivery` cho agent đó.

## Bước 6 — Dựng một agent mẫu (Deputy) end-to-end

Phía agent cần 3 thứ: script cầu phía VM, lính canh, và luật ứng xử phía agent (`agent/AGENT_SIDE.md`).

1. **Trên VM/máy chủ** (nơi agent SSH tới): đặt `agent/portainer_bridge.py` vào thư mục home. Script này đọc token Portainer tại chỗ (file `~/.secrets/portainer_token` phía máy chủ — token không rời máy chủ), exec vào container hub qua Portainer trên LAN. Sửa hằng số trong script cho khớp hạ tầng của bạn (địa chỉ Portainer, endpoint, tên container).
2. **Trên máy của agent**: chép `agent/sentinel.sh`, `agent/say.sh`, `agent/connect_via_proxy.py` vào thư mục làm việc của agent (ví dụ: `~/workspace/discord-bridge-agent/`).
3. **Thay danh tính** trong `sentinel.sh` và `say.sh` (chỉ thay các chỗ này, không sửa cơ chế):
   - `DIR`: thư mục làm việc của agent.
   - `KEY`: đường dẫn khóa SSH riêng của agent.
   - `HOST`: `<user>@<địa chỉ Tailscale của VM>` (Bước Tailscale ở phần điều kiện cần).
   - Mọi chỗ `--agent deputy` → tên agent của bạn; file lỗi tạm `agent_sentinel_ssh_err.txt` có thể giữ nguyên hoặc đổi tên tương ứng.
   - Nếu SSH đi thẳng được thì bỏ `ProxyCommand`; nếu phải qua proxy của runtime thì giữ `connect_via_proxy.py`.
4. **Chạy lính canh**: `bash sentinel.sh` dưới dạng tiến trình nền. Lính canh giữ một kết nối SSH, vòng lặp phía VM treo `wait --timeout 50`; có tin là gọi `working` cho tin đầu, in JSON rồi kết thúc — agent xử lý, trả lời bằng `say.sh` (`bridge say --final`), và **mở lại lính canh ngay trong cùng lượt**.
5. **Điểm danh thử**: trong phòng, chủ phòng gõ `"deputy điểm danh`. Đạt khi: lính canh kết thúc và đánh thức agent trong vài giây, agent trả lời chốt trong phòng, và lính canh mới đã được mở lại.

## Bước 7 — Nhân bản cho các agent còn lại

Lặp lại Bước 6 cho từng agent, mỗi agent một thư mục, một khóa SSH, một tên `--agent` riêng. Mỗi bản sao chỉ khác nhau ở đúng những dòng danh tính đã liệt kê ở Bước 6 — ngoài chúng ra các bản giống hệt nhau.

## Bước 8 — Luật phòng (tóm tắt)

Toàn văn ở `docs/02-luat-phong.md`. Nòng cốt:

- **FIX-6**: một tin chỉ là LỜI GỌI khi có dấu `"` ngay trước tên agent, hoặc @mention, hoặc chữ "tất cả". Nhắc tên không có dấu `"` = chỉ nhắc tới, agent không trả lời. Nhắc tới agent khác mà không giao việc trực tiếp thì viết tên thường, không dùng dạng gọi.
- Được gọi thì đọc kĩ, quan sát thứ tự trên bảng chat, làm đúng thứ tự.
- Agent xưng hô với nhau bằng tên; tối đa 8 tin bot–bot liên tiếp (hub có ngắt mạch); việc xong là im.
- Việc cần làm lâu: đăng **tin biên nhận trước**, rồi mới làm.
- Tin của chủ phòng chứa từ lệnh độc lập **"now"** = việc gấp, áp phương án nhanh nhất.
- Không đăng code lên bảng chat — code/nội dung dài đi qua file.

## Bước 9 — Vận hành

- **Nhật ký**: `/data/bridge.jsonl` trong container — mỗi dòng một JSON, các loại: `message`, `delivery`, `working`, `reply`, `warning`. Lọc/gom ngay trong container trước khi lấy ra (đầu ra exec qua Portainer bị cắt ở khoảng 4KB).
- **Ack 2 mức**: `working` = đã nhận (mức 1), gia hạn lease +5 phút/lần, trần 30 phút. `say`/`ack` chốt = mức 2. Tin chưa chốt được **giao lại tối đa 3 lần**.
- **Escalation / recovered**: quá 5 phút chưa xử lý xong, hub đăng cảnh báo vào phòng; quá 30 phút @chủ phòng. "Recovered" nghĩa là agent bị gắn cờ treo trước đó vừa chốt xong trao đổi — nó là triệu chứng của việc để trao đổi treo lâu, không phải lỗi của hub.
- **Các bẫy đã biết**: (1) các `--part` sau phần 1 chỉ là *sửa* cùng một tin, không route lại — lời gọi phải là tin mới; (2) giữ trao đổi mở lâu để làm "mỏ neo" sẽ bị gắn cờ treo; (3) mỗi số part chỉ gửi được một lần.
- **Quay lui**: nếu còn giữ cầu v1 (mỗi agent một container cầu file-based), quy trình lui là dừng hub v2 và start lại các container v1.

## Bước 10 — Hẹn giờ / tự thức

Toàn văn ở `docs/05-hen-gio-tu-thuc.md` và `docs/SELF_WAKE_PROPOSAL.md`.

- **Phương án A — cron trong runtime của agent**: job runonce tới giờ mở một lượt agent mới; thân job mang id tin lệnh gốc + nội dung cần đăng + nơi báo cáo. Kỷ luật: tạo job xong **đọc lại xác nhận ngay**; trước mốc quan trọng phải **test +2 phút**; quá giờ +2 phút chưa có tin = miss và phải **báo chủ động**.
- **Phương án B — đồng hồ trên VM (xương sống khuyến nghị)**: cron trên VM luôn bật quét một bảng hẹn JSON; tới giờ đăng tin route tới agent đích qua **đường đăng độc lập** (bot đăng trực tiếp qua Discord API — hub vẫn nạp và route theo dạng gọi), lính canh của agent bắt được trong khoảng 1 giây.
- **Phương án C — hub tự hẹn giờ**: hướng dài hạn, cần sửa hub, chưa làm.

## Bảo mật

- Token bot chỉ nằm trong **`hub.env` (chmod 600)** phía máy chủ và trong bộ nhớ của hub; token Portainer chỉ nằm trong file bí mật phía VM. Không token nào nằm trong repo này.
- `.gitignore` đã chặn: `hub.env`, `__pycache__/`, `*.pyc`, `hub_inbox.jsonl`, `.secrets/`, `*.log`. Trước khi push, tự quét lại cây repo tìm chuỗi giống token (chuỗi dài 40+ ký tự, tiền tố `ptr_`, chữ `PASSWORD`…) — xem `deploy/hub.env.example` để biết bản mẫu chỉ chứa tên biến.
- Các script trong `deploy/` là bản đã dùng thật cho hạ tầng tham chiếu (Docker host qua Portainer, máy chủ qua Tailscale) — đường dẫn và địa chỉ trong đó là của bản tham chiếu, cần chỉnh cho hạ tầng của bạn.
