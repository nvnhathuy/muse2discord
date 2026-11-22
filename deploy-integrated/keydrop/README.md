# Keydrop — trang quản trị tự thêm SSH key của agent lên VM điều phối

## Luồng hoạt động

```
Trang quản trị (trong container hub)
  │  ssh bằng khoá riêng của hub (hub_ssh_key, trong thư mục dữ liệu)
  │  stdin: "<agent-key>\n<public-key>\n"
  ▼
User "keydrop" trên VM ── authorized_keys khoá cứng bằng command=
  │  chỉ chạy được /usr/local/bin/agent-key-intake, không shell,
  │  không pty, không chuyển cổng
  ▼
agent-key-intake: kiểm tra định dạng thật chặt → ghi 1 dòng vào
  /var/lib/keydrop/pending.tsv  (dạng: agentkey<TAB>ssh-ed25519 <blob>)
  ▼
agent-key-install (root, systemd .path canh file pending):
  kiểm tra lại từ đầu → chống trùng theo blob → append vào
  /home/debian/.ssh/authorized_keys với comment "hub-agent:<agentkey>"
  → ghi log /var/log/agent-key-install.log
```

Nhờ vậy trang quản trị tự hoàn tất việc thêm key, không cần người
trung gian. Agent chỉ gửi PUBLIC key; private key của agent không bao
giờ rời máy của agent.

## Cài đặt trên VM (làm một lần, quyền root)

Mọi địa chỉ dưới đây là placeholder — thay bằng địa chỉ thật của VM
khi cài.

1. Tạo user và thư mục chứa file chờ:

   ```bash
   sudo useradd -m -s /bin/bash keydrop
   sudo install -d -o keydrop -g keydrop -m 750 /var/lib/keydrop
   ```

2. Cài hai script:

   ```bash
   sudo install -m 755 agent-key-intake agent-key-install /usr/local/bin/
   ```

3. Cài và bật systemd unit (installer chạy mỗi khi file pending xuất
   hiện hoặc thay đổi):

   ```bash
   sudo install -m 644 agent-key-install.service agent-key-install.path \
     /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now agent-key-install.path
   ```

4. Cấp quyền cho khoá SSH của hub vào tài khoản keydrop. Public key
   của hub nằm ở `hub_ssh_key.pub` trong thư mục dữ liệu của hub
   (theo `BRIDGE_REGISTRY_DIR` — hub tự sinh cặp khoá ở lần gửi key
   đầu tiên; nếu muốn lấy trước, chạy `ssh-keygen -t ed25519 -N "" -f
   hub_ssh_key` ngay trong thư mục đó). Ghi ĐÚNG MỘT dòng sau vào
   `/home/keydrop/.ssh/authorized_keys` (thay `<HUB_PUBLIC_KEY>` bằng
   toàn bộ dòng trong `hub_ssh_key.pub`):

   ```
   command="/usr/local/bin/agent-key-intake",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding <HUB_PUBLIC_KEY>
   ```

   Rồi siết quyền:

   ```bash
   sudo chown -R keydrop:keydrop /home/keydrop/.ssh
   sudo chmod 700 /home/keydrop/.ssh
   sudo chmod 600 /home/keydrop/.ssh/authorized_keys
   ```

5. Khai báo đích gửi cho hub: trong môi trường của container hub đặt

   ```
   HUB_KEYDROP_TARGET=keydrop@<địa chỉ VM>
   ```

   (mẫu đã có sẵn dạng chú thích trong `deploy-integrated/compose.yaml`).

## Kiểm tra sau khi cài

- Từ phía hub: mở trang quản trị → dòng agent → nút "SSH key" → dán
  public key của agent → bấm "Thêm key". Trang báo fingerprint là đã
  xong.
- Trên VM: `tail /var/log/agent-key-install.log` phải có dòng
  `ADD agent=<key> fp=SHA256:...`, và key xuất hiện trong
  `/home/debian/.ssh/authorized_keys` với comment `hub-agent:<key>`.
- Test độc lập hai script (không đụng hệ thống thật):
  `bash test_keydrop.sh` ngay trong thư mục này.

## An toàn

- Khoá của hub trên VM chỉ chạy được đúng script intake (forced
  command) — lộ khoá hub cũng không mở được shell trên VM.
- Intake chỉ nhận đúng 2 dòng, tối đa ~2KB: dòng 1 là khoá agent
  (`^[a-z0-9_-]{1,32}$`), dòng 2 là key ssh-ed25519; comment do agent
  gửi bị cắt bỏ, hệ thống tự đặt comment `hub-agent:<key>`.
- Installer (root) kiểm tra lại toàn bộ, chống trùng theo blob khoá,
  giữ authorized_keys ở quyền 600 và chủ `debian`.
- Muốn thu hồi một agent: xoá dòng có comment `hub-agent:<key>` khỏi
  `/home/debian/.ssh/authorized_keys`.
