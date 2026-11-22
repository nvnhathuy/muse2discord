#!/bin/bash
# test_keydrop.sh — test tự chứa cho agent-key-intake + agent-key-install.
# Mọi thứ chạy trong thư mục tạm qua các biến KEYDROP_*; KHÔNG đụng vào
# hệ thống thật. Chạy:  bash test_keydrop.sh
set -u
cd "$(dirname "$0")"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export KEYDROP_PENDING_FILE="$TMP/pending.tsv"
export KEYDROP_AUTH_KEYS="$TMP/authorized_keys"
export KEYDROP_LOG_FILE="$TMP/install.log"
export KEYDROP_AUTH_OWNER="$(id -un)"

pass=0; fail=0
check() { local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then pass=$((pass+1)); echo "ok   - $desc"
  else fail=$((fail+1)); echo "FAIL - $desc"; fi
}

# Fixture: một khoá ed25519 thật.
ssh-keygen -t ed25519 -N "" -C "agent-laptop" -f "$TMP/agent_key" -q
PUB="$(cat "$TMP/agent_key.pub")"
BLOB="$(awk '{print $2}' "$TMP/agent_key.pub")"
SHORT_BLOB="AAAAC3NzaC1lZDI1NTE5AAAA"   # quá ngắn so với blob thật

intake() { printf '%s' "$1" | bash ./agent-key-intake; }
pending_sum() { md5sum "$KEYDROP_PENDING_FILE" 2>/dev/null | awk '{print $1}'; }

# ------------------------------------------------------------- intake
intake "testagent
$PUB
"; rc=$?
check "intake nhận key hợp lệ (rc=0)" test $rc -eq 0
printf 'testagent\tssh-ed25519 %s\n' "$BLOB" > "$TMP/expected"
check "pending đúng dạng agentkey<TAB>ssh-ed25519 blob (comment bị cắt)" \
  cmp -s "$TMP/expected" "$KEYDROP_PENDING_FILE"

before="$(pending_sum)"
intake "Bad Key!
$PUB
"; rc=$?
check "intake từ chối agent key sai định dạng" test $rc -ne 0
intake "testagent
ssh-rsa $BLOB
"; rc=$?
check "intake từ chối key không phải ssh-ed25519" test $rc -ne 0
intake "testagent
ssh-ed25519 $SHORT_BLOB
"; rc=$?
check "intake từ chối blob quá ngắn" test $rc -ne 0
intake "testagent
$PUB
ssh-ed25519 $BLOB dong-chen-them
"; rc=$?
check "intake từ chối payload 3 dòng (chèn dòng)" test $rc -ne 0
intake ""; rc=$?
check "intake từ chối input rỗng" test $rc -ne 0
check "pending không đổi sau mọi payload rác" test "$(pending_sum)" = "$before"

# ------------------------------------------------------------ install
bash ./agent-key-install; rc=$?
check "installer chạy xong (rc=0)" test $rc -eq 0
check "authorized_keys có key với comment hệ thống hub-agent:<key>" \
  grep -qxF "ssh-ed25519 $BLOB hub-agent:testagent" "$KEYDROP_AUTH_KEYS"
check "authorized_keys có quyền 600" \
  test "$(stat -c %a "$KEYDROP_AUTH_KEYS")" = "600"
check "pending đã bị xoá sau khi cài" test ! -f "$KEYDROP_PENDING_FILE"
check "log có dòng ADD kèm fingerprint" \
  grep -q "ADD agent=testagent fp=SHA256:" "$KEYDROP_LOG_FILE"

# Chống trùng: nộp lại đúng key đó, cài lại — vẫn chỉ 1 dòng.
intake "testagent
$PUB
"
bash ./agent-key-install
count="$(grep -c "$BLOB" "$KEYDROP_AUTH_KEYS")"
check "cài lại cùng key không thêm dòng mới (chống trùng theo blob)" \
  test "$count" = "1"
check "log có dòng SKIP duplicate" grep -q "SKIP duplicate" "$KEYDROP_LOG_FILE"

# Dòng rác trong pending bị bỏ qua, dòng tốt vẫn được cài.
ssh-keygen -t ed25519 -N "" -C "second" -f "$TMP/agent2" -q
BLOB2="$(awk '{print $2}' "$TMP/agent2.pub")"
printf 'DONG RAC KHONG HOP LE\nsecondagent\tssh-ed25519 %s\n' "$BLOB2" \
  > "$KEYDROP_PENDING_FILE"
bash ./agent-key-install
check "installer bỏ qua dòng rác, vẫn cài dòng tốt" \
  grep -qxF "ssh-ed25519 $BLOB2 hub-agent:secondagent" "$KEYDROP_AUTH_KEYS"

echo
echo "keydrop tests: $pass ok, $fail fail"
[ "$fail" -eq 0 ]
