#!/bin/bash
# 安裝《Les Misérables》票務監控：設定 Gmail、寄測試信、每 5 分鐘自動執行（macOS launchd）
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/Library/Application Support/LesMisWatcher"   # 放在這裡避免 macOS 對「文件」資料夾的權限限制
LABEL="com.lesmis-watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY=/usr/bin/python3
INTERVAL_SEC="${INTERVAL_SEC:-300}"

mkdir -p "$APP_DIR"
cp "$SRC_DIR/les_mis_watcher.py" "$APP_DIR/"

CONFIG="$APP_DIR/config.json"
if [ ! -f "$CONFIG" ] || [ "${RECONFIGURE:-0}" = "1" ]; then
  echo "=== Gmail 設定 ==="
  echo "需要一組 Gmail「應用程式密碼」（不是你的 Gmail 登入密碼）。"
  echo "建立方式：https://myaccount.google.com/apppasswords （帳號需先開啟兩步驟驗證）"
  echo
  read -r -p "寄件 Gmail 帳號: " SMTP_USER
  read -r -s -p "16 碼應用程式密碼（輸入時不會顯示）: " APP_PW; echo
  read -r -p "通知寄到哪個信箱 [同寄件帳號]: " NOTIFY_TO
  NOTIFY_TO="${NOTIFY_TO:-$SMTP_USER}"
  read -r -p "只要青年票？（n = 青年票或任何 < €30 的票）[Y/n]: " YO
  case "$YO" in [nN]*) MODE=youth_or_under30 ;; *) MODE=youth ;; esac
  umask 077
  SMTP_USER="$SMTP_USER" APP_PW="$APP_PW" NOTIFY_TO="$NOTIFY_TO" MODE="$MODE" CONFIG="$CONFIG" \
  "$PY" - <<'EOF'
import json, os
json.dump({
    "smtp_host": "smtp.gmail.com", "smtp_port": 465,
    "smtp_user": os.environ["SMTP_USER"],
    "smtp_app_password": os.environ["APP_PW"].replace(" ", ""),
    "notify_to": os.environ["NOTIFY_TO"],
    "mode": os.environ["MODE"],
}, open(os.environ["CONFIG"], "w"), indent=2)
EOF
  chmod 600 "$CONFIG"
  unset APP_PW
fi

echo "=== 寄送測試信 ==="
"$PY" "$APP_DIR/les_mis_watcher.py" --test-email

echo "=== 安裝自動排程（每 $((INTERVAL_SEC / 60)) 分鐘檢查一次）==="
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$APP_DIR/les_mis_watcher.py</string>
  </array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>StartInterval</key><integer>$INTERVAL_SEC</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$APP_DIR/watcher.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/watcher.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "✅ 完成！監控已啟動，第一次檢查正在執行（約 1 分鐘），符合條件的票會寄到你的信箱。"
echo "   查看紀錄： tail -f \"$APP_DIR/watcher.log\""
echo "   停止監控： bash \"$SRC_DIR/uninstall.sh\""
