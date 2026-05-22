# 上線部署說明

此版本已支援公開主機部署：

- 啟動主機：`0.0.0.0`
- 連接埠：自動讀取雲端平台的 `PORT`
- 跨網域：使用 `STOCK_V1_ALLOWED_ORIGINS` 控制 CORS
- 啟動指令：`python -m stock_v1 web --host 0.0.0.0`

## 最快上線方式：Render

1. 將專案上傳到 GitHub。
2. 到 Render 建立 `New Web Service`。
3. 選擇此 GitHub 專案。
4. Build command：

```bash
pip install -r requirements.txt
```

5. Start command：

```bash
python -m stock_v1 web --host 0.0.0.0
```

6. Environment variables：

```text
STOCK_V1_ALLOWED_ORIGINS=*
```

7. 若要綁定自己的網域，在 Render 的 `Custom Domains` 加入網域，然後到網域商設定 CNAME。

## Railway / Zeabur / VPS

啟動指令相同：

```bash
python -m stock_v1 web --host 0.0.0.0
```

若平台需要 Docker，直接使用專案內的 `Dockerfile`。

## 資料庫

目前系統使用 SQLite：

```text
data/tw_stocks.sqlite
```

完整本機資料庫不建議直接上傳 GitHub。部署第一版使用輕量資料庫：

```text
data/tw_stocks_deploy.sqlite
```

重新產生部署資料庫：

```bash
python -m stock_v1.deploy_db --days 365
```

Render 請加入：

```text
STOCK_V1_DB_PATH=data/tw_stocks_deploy.sqlite
STOCK_V1_PUBLIC_DEMO=0
STOCK_V1_TELEGRAM_BOT_TOKEN=你的 Telegram Bot Token
```

`render.yaml` 已設定 `plan: free`，Render 會提供固定網址：

```text
https://taiwan-stock-ai.onrender.com
```

若此名稱已被使用，Render 會要求改服務名稱，網址也會跟著改。

正式營運時建議改成：

- 主機持久化硬碟，保留 SQLite。
- 或升級成 PostgreSQL，方便網站、APP、排程與推播共用資料。

## 安全提醒

不要把 `config/notify.json` 上傳到公開 GitHub，裡面可能有 Telegram / LINE token。

## Telegram 問答 Bot

正式版 Render 使用 Webhook：

```bash
python -m stock_v1 telegram-webhook --url https://你的-render網址/api/telegram/webhook
```

本機版使用 Polling：

```powershell
python -m stock_v1 telegram-delete-webhook
python -m stock_v1 telegram-poll
```

同一個 Telegram Bot 同一時間只能使用一種收訊方式。若要切回 Render 正式版：

```bash
python -m stock_v1 telegram-webhook --url https://你的-render網址/api/telegram/webhook
```

可在 Telegram 輸入：

```text
2330
分析 2454
加入 2367
移除 2367
我的觀察名單
AI智選
幫助
```

正式版與本機版都需要設定 Telegram Bot Token：

```text
STOCK_V1_TELEGRAM_BOT_TOKEN=你的 Telegram Bot Token
```

本機也可以繼續使用：

```text
config/notify.json
```
