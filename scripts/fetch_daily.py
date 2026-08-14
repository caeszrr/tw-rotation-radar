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
# RADAR_SIMULATE_DATE 只給演練用（模擬休市日跑一次管線）。正常執行絕不設定，
# 設定時會在 log 印出明顯標記，避免有人把演練輸出當成正式紀錄。
_SIM = os.environ.get("RADAR_SIMULATE_DATE", "").strip()
RUN_ID = _SIM or datetime.now(TPE).strftime("%Y-%m-%d")
HOLIDAYS = os.path.join(ROOT, "data", "holidays.json")

# Phase 6 素材（決策 #27 條件④，2026-08-14 實測，本輪【不動排程】）：
# 整批端點 STOCK_DAY_ALL 收盤後落後 14-16 小時（20:15 仍是前一交易日），
# 但**逐檔月表** https://www.twse.com.tw/exchangeReport/STOCK_DAY?date=YYYYMM01&stockNo=CODE
# 當天 20:15 就已經有當日資料（scripts/verify_finmind_alignment.py 實際拿它比對成功）。
# 代價是 229 次逐檔呼叫 vs 1 次整批。燒機 5 日量完更新時點後，一併納入決策 #23 的排程重議。
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


def soft_json(url, label):
    """
    官方端點的「軟」取用：打不通就回 None，不 raise（決策 #27 / R1）。

    只給那四個**有 FinMind 備援可以接手**的來源用。沒有備援的呼叫仍走 get_json()，
    該死就死 —— 不要把這個函式當成通用的「吞例外」工具。

    RADAR_SIMULATE_DOWN 只給演練用（逗號分隔的來源名稱，或 `all`），
    與 RADAR_SIMULATE_DATE 同一套紀律：設定時印明顯標記，正常執行絕不設定。
    """
    sim_down = os.environ.get("RADAR_SIMULATE_DOWN", "").strip()
    if sim_down and (sim_down == "all" or label in [s.strip() for s in sim_down.split(",")]):
        log("L0-SIMULATE", f"演練：強制讓官方端點 {label} 打不通（RADAR_SIMULATE_DOWN）")
        return None
    try:
        return get_json(url)
    except Exception as e:                                   # noqa: BLE001
        log("L2-down", f"官方端點 {label} 打不通（已重試）：{e}")
        return None


def notify_degraded(down, day):
    """
    條件③：降級絕不沉默。走既有的 notify.telegram（Actions 裡本來就有 secret），
    不新增任何憑證。送不出去也不阻擋管線 —— 但會在日誌留下痕跡。
    """
    try:
        sys.path.insert(0, HERE)
        import notify
        notify.telegram(
            f"🟠 台股輪動雷達 日線管線降級（L1-degraded-full-finmind）\n"
            f"{day}：官方端點打不通 {down}\n"
            f"已改由 FinMind 補齊該部分。完整性閘門（{len(expected_universe())} 檔 / "
            f"{MIN_COVERAGE:.0%}）照常把關，不合格仍會拒絕發布。")
    except Exception as e:                                   # noqa: BLE001
        log("L2-warn", f"降級通知送出失敗（不阻擋管線）：{e}")


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


def expected_universe():
    """
    預期宇宙 = sectors.json 全部成分股 + 規模分頁 ETF + 兩條報酬指數。
    **不可**用 prices.csv 內現有代號當分母 —— 檔案若被截斷，分母會跟著縮小，
    覆蓋率反而算出 100%+ 而放行（2026-08-13 演練實際踩到）。
    """
    with open(os.path.join(ROOT, "data", "sectors.json"), encoding="utf-8") as f:
        sec = json.load(f)
    u = {c for v in sec["sectors"].values() for c in v}
    u |= {"0050", "0051", "006201", "0056", "00713", "00692"}
    u |= {"TAIEX_TR", "TPEX_TR"}
    return u


def holiday_map():
    """
    讀 data/holidays.json → {'YYYY-MM-DD': 名稱}。

    **格式紀律（2026-08-13）**：holidays.py 寫出的是**物件陣列** `[{date,name,desc}]`。
    Worker 那邊曾因為假設它是字串陣列而讓休市日防護整條失效，且測試餵假格式所以全綠。
    這裡一律以真實檔案格式為準，並同樣容忍字串陣列（不製造第二套假設）。
    檔案不存在/壞掉時回空 dict —— 不阻擋管線（與 holidays.py 的 L7 紀律一致）。
    """
    try:
        with open(HOLIDAYS, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:                                   # noqa: BLE001
        log("L7-warn", f"讀不到休市日曆（不阻擋）：{e}")
        return {}
    items = raw.get("dates", raw) if isinstance(raw, dict) else raw
    out = {}
    for r in items or []:
        if isinstance(r, str):
            out[r[:10]] = ""
        elif isinstance(r, dict) and r.get("date"):
            out[str(r["date"])[:10]] = r.get("name") or r.get("desc") or ""
    return out


def read_prices():
    rows, codes = [], set()
    with open(PRICES, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
            codes.add(r["code"])
    return rows, codes


def main():
    if _SIM:
        log("L0-SIMULATE", f"演練模式：RADAR_SIMULATE_DATE={_SIM}（正常執行不會出現這一行）")
    log("L1-start", "每日增量開始")

    # ── L7 休市日：成功 + 心跳 + 零告警（藍圖第九節）──────────────
    # 放在所有網路呼叫【之前】：休市日應該零請求、零成本、零雜訊。
    # 在此之前，管線根本沒有休市日分支 —— 休市日是靠「官方端點還是昨天的資料 →
    # day <= latest → L7-noop」間接達成的，也就是說它和冪等走同一條路徑，
    # 兩者無法分辨；holidays.py 抓來的日曆從來沒有任何程式讀過。
    hol = holiday_map()
    if RUN_ID in hol:
        log("L7-holiday", f"{RUN_ID} 為官方休市日（{hol[RUN_ID] or '無名稱'}）"
                          f"→ 不抓取、不寫入、不發布，正常結束（心跳照發、零告警）")
        return 0
    log("L7-ok", f"{RUN_ID} 非官方休市日（日曆共 {len(hol)} 筆）")

    rows, present = read_prices()
    tracked = expected_universe()
    # L3-a：先驗「歷史檔本身」是否完整——截斷的 prices.csv 必須當場擋下
    miss_hist = sorted(tracked - present)
    if len(present & tracked) < MIN_COVERAGE * len(tracked):
        log("L3-GATE-FAIL", f"prices.csv 歷史不完整：預期 {len(tracked)} 檔，實際只有 "
                            f"{len(present & tracked)} 檔（缺 {len(miss_hist)}）→ 不發布")
        sys.exit(5)
    if miss_hist:
        log("L3-gate", f"（可接受）歷史檔缺 {len(miss_hist)} 檔：{miss_hist[:10]}")
    have = defaultdict(set)
    for r in rows:
        have[r["code"]].add(r["date"])
    latest = max(r["date"] for r in rows)
    log("L1-state", f"追蹤 {len(tracked)} 檔，現有最新日期 {latest}，共 {len(rows)} 列")

    # ── 官方端點：打不通不再讓整條管線陪葬（決策 #27 / R1）────────────────
    #
    # 2026-08-14 20:02 的排程跑就死在這裡：STOCK_DAY_ALL 連 5 次回非 JSON，
    # get_json() raise，管線整條崩潰 —— 而 FinMind 手上其實有完整資料
    # （6 分鐘後的補跑用它補回 165/165）。舊寫法把「來源落後」與「來源打不通」
    # 混為一談：只有前者有備援，後者直接死。
    #
    # 現在兩者分開處理：
    #   落後(stale) → 該來源不參與填值，缺的由 FinMind 補（原本就有的行為）
    #   打不通(down) → 同上，另外標 L1-degraded-full-finmind 並【當次】發 Telegram
    # **完整性閘門 231 檔 / 95% 完全不因降級放寬**（條件②）——降級只換來源，不換標準。
    twse = soft_json(U_TWSE, "TWSE個股")
    tpex = soft_json(U_TPEX, "TPEx個股")
    mi = soft_json(U_MI, "加權報酬")
    tptr = soft_json(U_TPEXTR, "櫃買報酬")

    d_twse = roc_to_iso(twse[0]["Date"]) if twse else None
    d_tpex = roc_to_iso(tpex[0]["Date"]) if tpex else None
    d_mi = roc_to_iso(mi[0]["日期"]) if mi else None
    d_tptr = roc_to_iso(tptr[-1]["Date"]) if tptr else None
    srcs = (("TWSE個股", twse, d_twse), ("TPEx個股", tpex, d_tpex),
            ("加權報酬", mi, d_mi), ("櫃買報酬", tptr, d_tptr))
    log("L1-fetch", "　".join(f"{n}({d or 'DOWN'})" for n, _, d in srcs))

    down = [n for n, v, _ in srcs if v is None]
    dates = [d for _, _, d in srcs if d]

    if not dates:
        # 四個全掛 → 連「目標日是哪一天」都問不到。用台北今日 + 休市日曆推定；
        # 推不出交易日就安靜結束（不寫入、不告警——那是週末/休市的正常情形）。
        hol = holiday_map()
        wd = datetime.strptime(RUN_ID, "%Y-%m-%d").weekday()      # 0=一 … 6=日
        if wd >= 5 or RUN_ID in hol:
            log("L7-noop", f"四個官方端點全部打不通，且 {RUN_ID} 非交易日（{'週末' if wd >= 5 else hol.get(RUN_ID) or '休市日'}）"
                           f" → 不推定目標日，冪等結束")
            return 0
        day = RUN_ID
        log("L1-degraded-full-finmind",
            f"四個官方端點全部打不通 {down} → 目標日依台北交易日推定為 {day}，全部改由 FinMind 補")
    else:
        day = max(dates)
        if down:
            log("L1-degraded-full-finmind",
                f"官方端點打不通 {down} → 該部分改由 FinMind 全補（目標日 {day} 由其餘來源決定）")

    stale = [n for n, v, d in srcs if v is not None and d != day]
    if stale:
        log("L1-stale", f"目標日 {day}，落後的官方來源：{stale} → 將以 FinMind 備援補齊")
    if day <= latest:
        log("L7-noop", f"{day} 已在庫（或非交易日尚未更新），不重複寫入。冪等結束。")
        return 0
    # 條件③：降級絕不沉默。閘門若接著擋下，工作流本來就會再發一則失敗告警，兩則不衝突。
    if down:
        notify_degraded(down, day)

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
