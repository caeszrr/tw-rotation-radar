# 盤中即時層 Worker（Phase 4.5）

每分鐘由 Cron 觸發；程式內判斷「台北交易日 09:00–13:30」才實際抓取，
其餘時間立即 return（省免費額度）。

## 它做什麼

1. 分批抓 `mis.twse.com.tw/stock/api/getStockInfo.jsp`
   （上市 `tse_XXXX.tw`、上櫃 `otc_XXXX.tw`、指數 `tse_t00.tw`／`otc_o00.tw`）
2. **只寫「原始最新價」JSON 進 KV**，不做任何計算
3. 重計算放前端（避開 Worker 10ms CPU 上限）

## Phase 0 實測依據（都是量出來的，不是猜的）

- **不需要先取 session**：冷請求即回即時資料（HTTP 200）
- **安全批量 = 100 檔/請求**：150 可過但延遲跳到 2,079ms；200 回誤導性的
  `rtcode 9999 參數不足`；300 以上直接 HTTP 414 → **真正的限制是 URL 長度，不是檔數**
- **節流 = 每 5 秒 ≤3 請求**：實測 300 檔 × 3 輪共 900 列零失敗、未鎖 IP，每輪 5.1 秒
- **個股 `z`（最近成交價）只有 12% 有值** → 暫定價一律用（最佳買+最佳賣）/2；
  指數的 `z` 正常，可直接使用

## 它讀什麼（KV 的兩把鑰匙）

`tick()` 開工前會讀兩個 KV key，**兩個都必須先餵**，否則 Worker 會每分鐘回
`{error:'KV 缺少 universe'}` —— 不崩潰、看起來正常，但一筆報價都不會有。

| KV key | 內容 | 由誰寫 |
|---|---|---|
| `universe` | `{ch:[...231 筆 ex_ch...], market:{...}}` | `scripts/feed_kv.py` |
| `holidays` | `data/holidays.json` 原樣（物件陣列 `{date,name,desc}`） | `scripts/feed_kv.py` |

`holidays` 的格式相容性：`holidaySet()` 同時接受物件陣列、字串陣列與裸陣列。
**2026-08-13 修正**：原本硬寫 `hol.dates.includes(ymd)`，對真實檔案（物件陣列）永遠比對不中，
休市日防護等同不存在；舊測試餵字串所以全綠。現在測試直接讀 `data/holidays.json` 本尊，
並有一條迴歸鎖釘住「舊寫法必失敗」。

## 部署（需要 Cloudflare 帳號，步驟見 MORNING_TODO.md）

```bash
cd worker
npx wrangler login                          # 瀏覽器授權
npx wrangler kv namespace create QUOTES     # 取得 id 填進 wrangler.toml
npx wrangler deploy

# ↓ 部署後必做：餵 KV（可重跑，覆蓋語意）
cd ..
python scripts/feed_kv.py --dry-run         # 先看產出的 231 筆對不對
python scripts/feed_kv.py                   # 真的寫進 KV

# ↓ 選配：盤中心跳（Healthchecks 的 tw-radar-intraday Ping URL）
cd worker
npx wrangler secret put HC_PING_URL_INTRADAY
```

`feed_kv.py` 的市場別（`tse_` / `otc_` 前綴）**是打官方清單量出來的，不是猜的**；
任何一檔在兩份清單都找不到就當場 `exit 3` 中止，絕不預設某一邊 —— 猜錯前綴的後果是
那一檔永遠沒報價，而且不會有任何錯誤訊息。

未設 `HC_PING_URL_INTRADAY` 時，Worker 會印一行 `L2-warn` 然後照常運作（與 `notify.py` 同紀律）。
心跳只在**真的寫進 KV 之後**才發，且抓到 0 檔時發的是 `/fail` —— 不謊報平安。

驗證：`curl <worker>/health`（看盤中視窗判斷）、`curl <worker>/latest`（看 KV 內容）。

憑證只放本機環境變數與 CF Secret，**永不入 repo**。
