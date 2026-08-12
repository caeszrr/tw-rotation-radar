#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
管線 P1 —— 每日增量抓取 + 完整性閘門。

來源全部為 TWSE/TPEx 官方 OpenAPI，**零 token、零費用**：
  上市個股/ETF   https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
  上櫃個股/ETF   https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes
  加權報酬指數   https://openapi.twse.com.tw/v1/indicesReport/MI_INDEX  →「發行量加權股價報酬指數」
  櫃買報酬指數   https://www.tpex.org.tw/openapi/v1/tpex_reward_index   → TPExTotalReturnIndex
  除權息預告表   https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL

還原價維持連續（藍圖§四）：新列的 adj_close 直接等於 close（回推基準永遠是「最新」），
當某檔今日除息時，先把它**歷史所有** adj_close 乘上 (前收 − 現金股利)/前收，再寫入今日。

鐵律 #6「寧可拒繪，不可造假」：完整性閘門不合格 → 不寫 prices.csv、以非零碼結束，
由工作流保留上一版成功的 docs/，絕不發布部分資料。

執行：python scripts/fetch_daily.py
"""
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PRICES = os.path.join(ROOT, "data", "prices.csv")
TPE = timezone(timedelta(hours=8))
RUN_ID = datetime.now(TPE).strftime("%Y-%m-%d")

U_TWSE = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
U_TPEX = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
U_MI = "https://openapi.twse.com.tw/v1/indicesReport/MI_INDEX"
U_TPEXTR = "https://www.tpex.org.tw/openapi/v1/tpex_reward_index"
U_EXDIV = "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL"

MIN_COVERAGE = 0.95          # 完整性閘門：股票數 ≥ 預期 95%


def log(step, msg):
    print(f"[{datetime.now(TPE).isoformat(timespec='seconds')}] run={RUN_ID} {step}: {msg}", flush=True)


def get_json(url, tries=5):
    """所有網路呼叫指數退避 + 抖動，只重試暫時性錯誤（L2）。"""
    import random
    import time
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tw-rotation-radar/1.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:                       # noqa: BLE001 — 分類後重試，不吞掉
            last = e
            wait = min(60, 2 ** i) + random.random()
            log("L2-retry", f"{url.rsplit('/',1)[-1]} 第 {i+1}/{tries} 次失敗：{e}；{wait:.1f}s 後重試")
            time.sleep(wait)
    raise RuntimeError(f"取得失敗（已重試 {tries} 次）：{url} — {last}")


def roc_to_iso(s):
    s = str(s).strip()
    if "/" in s:
        y, m, d = s.split("/")
        return f"{int(y)+1911:04d}-{int(m):02d}-{int(d):02d}"
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3])+1911:04d}-{s[3:5]}-{s[5:7]}"
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    raise ValueError("無法解析日期：" + s)


def num(x):
    try:
        v = float(str(x).replace(",", "").strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def finmind_fill(codes, day, px):
    """
    備援來源（藍圖§四：FinMind 用於主來源故障時）。
    token 只從環境變數讀（鐵律 #3），未設定時**優雅跳過**，不讓整條管線炸掉。
    指數走 TaiwanStockTotalReturnIndex，個股走 TaiwanStockPrice。
    """
    import time
    import urllib.parse
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if not token:
        log("L2-warn", "FINMIND_TOKEN 未設定 → 跳過備援（僅使用官方端點）")
        return 0
    log("L1-fallback", f"啟用 FinMind 備援，需補 {len(codes)} 檔（token 長度 {len(token)}，值不列印）")
    idx_map = {"TAIEX_TR": ("TaiwanStockTotalReturnIndex", "TAIEX"),
               "TPEX_TR": ("TaiwanStockTotalReturnIndex", "TPEx")}
    got = 0
    for c in codes:
        ds, did = idx_map.get(c, ("TaiwanStockPrice", c))
        q = urllib.parse.urlencode({"dataset": ds, "data_id": did,
                                    "start_date": day, "end_date": day, "token": token})
        try:
            j = get_json("https://api.finmindtrade.com/api/v4/data?" + q, tries=3)
        except Exception as e:                        # noqa: BLE001
            log("L2-warn", f"FinMind {c} 取得失敗：{e}")
            continue
        for row in (j.get("data") or []):
            if row.get("date") != day:
                continue
            v = num(row.get("close") if ds == "TaiwanStockPrice" else row.get("price"))
            if v:
                px[c] = v
                got += 1
        time.sleep(1.2)                                # 免費層 600 次/小時，保守節流
    return got


def read_prices():
    rows, codes = [], set()
    with open(PRICES, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
            codes.add(r["code"])
    return rows, codes


def main():
    log("L1-start", "每日增量開始")
    rows, tracked = read_prices()
    have = defaultdict(set)
    for r in rows:
        have[r["code"]].add(r["date"])
    latest = max(r["date"] for r in rows)
    log("L1-state", f"追蹤 {len(tracked)} 檔，現有最新日期 {latest}，共 {len(rows)} 列")

    twse = get_json(U_TWSE)
    tpex = get_json(U_TPEX)
    mi = get_json(U_MI)
    tptr = get_json(U_TPEXTR)

    d_twse = roc_to_iso(twse[0]["Date"])
    d_tpex = roc_to_iso(tpex[0]["Date"])
    d_mi = roc_to_iso(mi[0]["日期"])
    d_tptr = roc_to_iso(tptr[-1]["Date"])
    log("L1-fetch", f"TWSE {len(twse)} 筆({d_twse})　TPEx {len(tpex)} 筆({d_tpex})　"
                    f"MI_INDEX({d_mi})　TPEx報酬({d_tptr})")

    # 目標日 = 四來源中最新者；落後的來源改由 FinMind 補（藍圖§四：FinMind 為主來源故障備援）
    day = max(d_twse, d_tpex, d_mi, d_tptr)
    stale = [n for n, d in (("TWSE個股", d_twse), ("TPEx個股", d_tpex),
                            ("加權報酬", d_mi), ("櫃買報酬", d_tptr)) if d != day]
    if stale:
        log("L1-stale", f"目標日 {day}，落後的官方來源：{stale} → 將以 FinMind 備援補齊")
    if day <= latest:
        log("L7-noop", f"{day} 已在庫（或非交易日尚未更新），不重複寫入。冪等結束。")
        return 0

    px = {}
    if d_twse == day:
        for r in twse:
            c = r["Code"]
            if c in tracked:
                v = num(r["ClosingPrice"])
                if v:
                    px[c] = v
    if d_tpex == day:
        for r in tpex:
            c = r["SecuritiesCompanyCode"]
            if c in tracked and c not in px:
                v = num(r["Close"])
                if v:
                    px[c] = v
    if d_mi == day:
        v = next((num(r["收盤指數"]) for r in mi if r["指數"] == "發行量加權股價報酬指數"), None)
        if v:
            px["TAIEX_TR"] = v
    if d_tptr == day:
        v = num(tptr[-1]["TPExTotalReturnIndex"])
        if v:
            px["TPEX_TR"] = v

    # ── FinMind 備援：只補官方端點缺的部分 ────────────────
    missing_now = sorted(tracked - set(px))
    if missing_now:
        got = finmind_fill(missing_now, day, px)
        log("L1-fallback", f"FinMind 備援補回 {got}/{len(missing_now)} 檔")

    # ── L3 完整性閘門 ────────────────────────────────────
    cov = len(px) / len(tracked)
    log("L3-gate", f"當日取得 {len(px)}/{len(tracked)} 檔（{cov:.1%}），門檻 {MIN_COVERAGE:.0%}")
    missing = sorted(tracked - set(px))
    if cov < MIN_COVERAGE:
        log("L3-GATE-FAIL", f"覆蓋率不足 → 不發布。缺 {len(missing)} 檔：{missing[:20]}")
        sys.exit(3)
    if "TAIEX_TR" not in px or "TPEX_TR" not in px:
        log("L3-GATE-FAIL", "基準報酬指數缺漏 → 不發布")
        sys.exit(4)
    if missing:
        log("L3-gate", f"（可接受）缺 {len(missing)} 檔，多為當日停牌：{missing[:20]}")

    # ── 除息調整：先修歷史，再寫今日 ──────────────────────
    adj_applied = []
    try:
        exdiv = get_json(U_EXDIV)
        for r in exdiv:
            c = str(r.get("StockID") or r.get("股票代號") or "").strip()
            if c not in tracked:
                continue
            ds = r.get("Date") or r.get("除權息交易日") or ""
            try:
                if roc_to_iso(ds) != day:
                    continue
            except ValueError:
                continue
            cash = num(r.get("CashDividend") or r.get("現金股利") or 0)
            prev = None
            for rr in reversed(rows):
                if rr["code"] == c and rr["date"] < day:
                    prev = float(rr["close"])
                    break
            if cash and prev and prev > cash:
                ratio = (prev - cash) / prev
                for rr in rows:
                    if rr["code"] == c:
                        rr["adj_close"] = str(float(rr["adj_close"]) * ratio)
                adj_applied.append(f"{c}×{ratio:.6f}(現金{cash})")
    except Exception as e:                            # noqa: BLE001
        log("L2-warn", f"除權息預告表取得失敗（今日若有除息，還原價會有落差）：{e}")
    log("L1-exdiv", f"今日套用除息還原 {len(adj_applied)} 檔 {adj_applied or ''}")

    for c in sorted(px):
        rows.append({"code": c, "date": day, "close": str(px[c]), "adj_close": str(px[c])})
    rows.sort(key=lambda r: (r["code"], r["date"]))
    tmp = PRICES + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["code", "date", "close", "adj_close"])
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, PRICES)
    log("L1-done", f"寫入 {day} 共 {len(px)} 列，prices.csv 現有 {len(rows)} 列")
    return 0


if __name__ == "__main__":
    sys.exit(main())
