# 台股輪動雷達 · Taiwan Sector Rotation Radar

把台股的產業族群與規模風格畫成**相對輪動圖（RRG）**——四象限＋彗星尾巴，
一眼看出資金正轉進哪裡、轉出哪裡。

**線上頁面**：GitHub Pages（由 `/docs` 供站，見本 repo 的 Pages 設定）

## 這是什麼

- 每個交易日 **17:50（台北）** 自動抓官方收盤 → 重算 → 發布靜態頁
- 基準採**報酬指數**（含息），與成分股的還原價同一調整基礎
- 產業 14 族群（含航運／金融兩個對照錨）+ 規模 7 分頁

## 計算規格 TWRRG-v1（公式凍結）

RRG 的官方 JdK 演算法為營業秘密，公開實作彼此在三個參數上並不一致
（樣本／母體標準差、動能置中 100 或 101、ROC 用滾動位移或固定基準）。
本專案一次鎖死這三個選擇，實作 `scripts/twrrg.py` 逐行對應下式，不含任何自由參數：

```python
RS          = 100 * (sector_close / benchmark_close)   # 兩者皆為還原價基礎
SMA_RS      = RS.rolling(14).mean()
SD_RS       = RS.rolling(14).std(ddof=1)               # 樣本標準差，非母體
RS_Ratio    = 100 + (RS - SMA_RS) / SD_RS              # 置中於 100
ROC         = 100 * (RS_Ratio / RS_Ratio.shift(10) - 1)  # 滾動位移 k=10
SMA_ROC     = ROC.rolling(14).mean()
SD_ROC      = ROC.rolling(14).std(ddof=1)
RS_Momentum = 100 + (ROC - SMA_ROC) / SD_ROC           # 置中於 100（不是 101）
```

- 日線；雙滾動視窗 `n=14`；ROC 回看 `k=10`；尾巴預設 5 點（UI 可調 3–20）
- 顯示視窗 20／60／120／240 交易日（**僅影響日期滑桿與回放範圍，不影響計算**）
- 族群指數 = **等權重日報酬鏈結**，基期 100：
  `Index_t = Index_{t-1} × (1 + mean(當日成分股報酬))`
  當日缺價的成分股不計入當日分母；成分股需在 `t` 與 `t-1` 皆有價才納入，
  絕不以跨日報酬冒充單日報酬
- 最低歷史 43 個交易日（14+10+14+5）；**不足 60 交易日的族群一律拒繪並標「歷史不足」**，
  絕不靜默繪出
- 四象限以 (100, 100) 為中心：領先／改善／落後／弱化

## 資料來源（全部公開、零費用、零 token）

| 用途 | 端點 |
|---|---|
| 上市個股/ETF 收盤 | `openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL` |
| 上櫃個股/ETF 收盤 | `www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes` |
| 加權股價**報酬**指數 | `openapi.twse.com.tw/v1/indicesReport/MI_INDEX` |
| 櫃買**報酬**指數 | `www.tpex.org.tw/openapi/v1/tpex_reward_index` |
| 除權息預告表 | `openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL` |

## 目錄

```
scripts/   twrrg.py（公式）、fetch_daily.py（增量+閘門）、build_history.py（重算）、test_twrrg.py
data/      prices.csv（code,date,close,adj_close 四欄）、sectors.json、shares_snapshot.json
docs/      index.html、plotly.min.js、history.json  ← GitHub Pages 根目錄
```

## 誠實標註的已知近似

- 還原價**僅處理現金股利**；股票股利與減資未還原
- 市值權重採**當期**發行股數快照，歷史增減資未還原
- 族群指數起算日不早於 **2023-12-12**（更早期間成分股覆蓋不足會失真）
- 「上櫃中小」（富櫃200指數）**無歷史來源**，自建置日起逐日累積，
  滿 60 交易日前一律標「資料累積中」不畫點

## 免責

**本頁僅呈現市場公開資料之計算結果，非投資建議。**
頁面只呈現象限、轉換與排名等事實，不含任何買賣指令用語。
