# Claude 用量小浮窗

待在螢幕角落的 Claude Code 用量監控小工具，不用再特地打開終端機查用量。
做法參考 [ccburn](https://github.com/JuanjoFuchs/ccburn)，浮窗操作沿用 `tomato_timer`。

![截圖](docs/screenshot-grok.png)

## 功能

- **兩個圓環**：左＝Claude 5 小時 Session 用量（橘），右＝Grok Bot 每週用量（白），深色底。
- **小條**：5h / 7d 兩條用量與距離重置的倒數。
- **單擊展開詳細資訊**：5 小時、每週（全部模型）、Sonnet、Opus、月度額度（有資料才顯示）、
  重置時間、用量速度與預估用完時間、本輪 5 小時走勢圖。
- **精簡模式**：右鍵選單切換，只顯示圓環、隱藏左/右側的小條與倒數（設定會記住）。
- 可拖曳、位置會記住；右鍵選單可重新整理、精簡模式、移到四個角落、結束。

## 需求

- Windows、Python 3.10+（只用標準庫，不需另外安裝套件）
- 已登入 Claude Code（`~/.claude/.credentials.json` 存在）

## 使用

雙擊 `usage.pyw`，或：

```powershell
pythonw usage.pyw
```

| 操作 | 作用 |
| --- | --- |
| 單擊 | 打開 / 關閉詳細資訊（Esc 也可關閉） |
| 拖曳 | 移動位置 |
| 右鍵 | 選單 |

## 打包成 exe

```powershell
pip install pyinstaller pillow
python make_icon.py          # 產生 usage.ico
python -m PyInstaller --onefile --noconsole --name ClaudeUsageWidget --icon usage.ico `
    --exclude-module PIL usage.pyw
```

輸出在 `dist\ClaudeUsageWidget.exe`（單一檔案、免安裝 Python）。

## 資料來源

1. **`~/.ccburn/history.db`** 最新一筆（ccburn 執行中，或 statusline 掛了 `ccburn collect` 時會持續更新），每 5 秒讀取。
2. 資料超過 5 分鐘沒更新時，改呼叫 OAuth usage API（`api.anthropic.com/api/oauth/usage`，讀取
   `~/.claude/.credentials.json` 的 access token）。最短間隔 3 分鐘；遇到 429 或錯誤會等 10 分鐘再試。

token 只用來呼叫上述 API，不會寫入任何檔案。設定（位置）存在 `%APPDATA%\ClaudeUsageWidget\data.json`。

可用 `CLAUDE_CONFIG_DIR` 指定其他 Claude 設定資料夾。

## Grok Bot 用量怎麼更新

Grok Bot 沒有公開的用量 API，所以資料是「推送 → 讀取」兩段：

1. **發布端（Grok Bot 例行工作）**：在 Grok Bot 裡建立一個每小時執行的 routine，讀取自己的 Grok 用量，
   把下面的 JSON 寫入一個 GitHub Gist（主要，用只開 gist 權限的 token），並發布到 ntfy.sh 主題與 sharehtml 頁面（備援）：

   ```json
   {"used_pct": 49.4, "resets_at": "2026-11-04T14:51:00Z", "updated_at": "2026-11-01T08:54:00Z"}
   ```

   - `used_pct`：本週已用百分比（0–100）
   - `resets_at`：每週額度重置時間（ISO 8601）
   - `updated_at`：這筆資料的產生時間

2. **讀取端（本浮窗）**：每 10 分鐘依序嘗試 `GROK_URLS`，取第一個成功的；回應中第一個 `{` 到最後一個 `}`
   會被當成 JSON 解析（所以 sharehtml 把 JSON 包在 HTML 裡也可以）。不需要任何金鑰。

**自己使用時**：把 `usage.pyw` 裡的 `GROK_URLS` 換成你自己的 Gist raw 網址（`https://gist.githubusercontent.com/<帳號>/<gist id>/raw/usage.json`）
和備援網址（ntfy.sh 公用伺服器會依 IP 限流，常回 429，所以只當備援），並讓你的 Grok Bot 依上面格式發布。ntfy 主題是公開的，知道名稱的人都能讀寫，請取一個難猜的名稱。
資料最多延遲約 1 小時（發布頻率）＋ 10 分鐘（讀取間隔）。
