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

## 部署（需要 Cloudflare 帳號，步驟見 MORNING_TODO.md）

```bash
cd worker
npx wrangler login                          # 瀏覽器授權
npx wrangler kv namespace create QUOTES     # 取得 id 填進 wrangler.toml
npx wrangler deploy
```

憑證只放本機環境變數與 CF Secret，**永不入 repo**。
