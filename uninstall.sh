#!/bin/bash
# 停止並移除《Les Misérables》票務監控的自動排程（設定檔與紀錄保留）
LABEL="com.lesmis-watcher"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "已停止監控" || echo "監控本來就沒有在執行"
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "設定與紀錄仍保留在：$HOME/Library/Application Support/LesMisWatcher"
