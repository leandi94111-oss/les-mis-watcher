# 《Les Misérables》Châtelet 青年票監控

在 GitHub 雲端每 5 分鐘（實際約 5–15 分鐘）檢查 Théâtre du Châtelet 的全部場次（2026/11/11–2027/01/10），
只要出現 **Jeune - de 30 ans 青年票**（官方售票，或官方轉售 Bourse aux billets 中的青年票），就寄 email 通知。
電腦關機也會繼續監控。演出結束（2027/01/10）後會自動停止檢查。

- 同一張票只通知一次；賣掉後再次釋出會再通知。
- 自動排除不符資格的票種（Enfant -15 ans、Carte Châtelet 會員價等）。

## 雲端設定（GitHub Actions）

Repo → Settings → Secrets and variables → Actions：

| Secret | 內容 |
|---|---|
| `SMTP_USER` | 用來寄信的 Gmail 帳號 |
| `SMTP_APP_PASSWORD` | 該 Gmail 的 16 碼應用程式密碼（https://myaccount.google.com/apppasswords） |
| `NOTIFY_TO` | 收通知的信箱 |

想改回「青年票或任何 < €30 的票」：在 Variables 新增 `LESMIS_MODE` = `youth_or_under30`。

- 手動執行一次：Actions → Les Mis youth ticket watcher → Run workflow
- 暫停監控：Actions → Les Mis youth ticket watcher → ⋯ → Disable workflow

## 本機執行（選用）

```bash
python3 les_mis_watcher.py --dry-run   # 只看結果，不寄信
bash setup.sh                          # 在這台 Mac 上每 5 分鐘執行（需開機）
bash uninstall.sh                      # 移除本機排程
```

## 限制

- 開演前約 15 分鐘的現場 last-minute 青年票無法線上監控。
- 若售票網站改版，解析可能失效（Actions 紀錄中會出現「找不到價格表」）。
