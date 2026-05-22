# Taiwan Stock Predictor V1

第一版先把「資料底座」做好：

- 自動抓台股上市 / 上櫃普通股清單
- 以 Yahoo Chart API 抓日 K
- 存進本機 SQLite
- 支援增量更新，不用每次重抓全部

> 這不是投資建議系統。V1 只建立資料庫與更新流程，後續版本再加入特徵、模型、回測與儀表板。

## 安裝

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 使用

初始化 / 更新股票清單：

```powershell
python -m stock_v1 universe
```

抓近 3 年所有上市櫃資料：

```powershell
python -m stock_v1 update --years 3
```

先試抓少數股票：

```powershell
python -m stock_v1 update --codes 2330,2317,6488 --years 3
```

查看資料庫狀態：

```powershell
python -m stock_v1 status
```

查詢單一股票：

```powershell
python -m stock_v1 stock 2330
```

查看技術指標：

```powershell
python -m stock_v1 indicators 2330
```

產生市場掃描報表：

```powershell
python -m stock_v1 scan --limit 20
```

產生訊號觀察名單：

```powershell
python -m stock_v1 signals --limit 20
```

每日 Top 5 觀察名單：

```powershell
python -m stock_v1 watchlist --limit 5
```

回測訊號模型：

```powershell
python -m stock_v1 backtest --top 10 --horizon 5 --step 5 --max-days 260
```

比較不同回測設定：

```powershell
python -m stock_v1 optimize --tops 5,10,20 --horizons 5,10,20 --step 5 --max-days 260
```

比較風險過濾效果：

```powershell
python -m stock_v1 risk-filter --top 5 --horizons 5,10,20 --step 5 --max-days 260
```

分析訊號條件貢獻：

```powershell
python -m stock_v1 features --horizon 10 --step 5 --max-days 260
```

策略資金曲線回測：

```powershell
python -m stock_v1 strategy --top 5 --horizon 20 --step 5 --max-days 260
```

真實持倉策略回測：

```powershell
python -m stock_v1 realistic-strategy --positions 5 --horizon 20 --step 5 --max-days 260 --cost-bps 20
```

匯出策略報表 CSV：

```powershell
python -m stock_v1 realistic-strategy --positions 5 --horizon 20 --step 5 --max-days 260 --cost-bps 20 --export
```

輸出位置：

```text
reports\
```

啟動本機網頁版：

```powershell
python -m stock_v1 web --port 8765
```

然後打開：

```text
http://127.0.0.1:8765
```

預覽每日推播內容：

```powershell
python -m stock_v1 notify-preview --limit 5
```

Telegram / LINE 推播設定：

```powershell
copy config\notify.example.json config\notify.json
```

填好 `config/notify.json` 後即可發送：

```powershell
python -m stock_v1 notify-telegram --limit 5
python -m stock_v1 notify-line --limit 5
```

每日一鍵流程：

```powershell
python -m stock_v1 daily-run --limit 5
```

只預覽、不發 Telegram：

```powershell
python -m stock_v1 daily-run --no-send --limit 5
```

查看最近執行紀錄：

```powershell
python -m stock_v1 runs
```

安裝 Windows 自動排程：

```text
install_daily_task.bat
```

排程時間：

```text
週一至週五 15:30
```

解除排程：

```text
uninstall_daily_task.bat
```

排程執行紀錄會放在：

```text
logs\
```

預設資料庫位置：

```text
data/tw_stocks.sqlite
```

## 下一版方向

- V1.1：加入技術指標欄位與資料品質檢查
- V1.2：新增簡單預測模型
- V2：加入 walk-forward backtest
- V3：多股票掃描與排名
