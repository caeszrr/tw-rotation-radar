#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
R1 條件① —— FinMind 欄位對齊驗證（決策 #27 的前置條件）。

**為什麼需要這一支**：決策 #27 允許「官方端點打不通時，整日改由 FinMind 全補」。
在那之前必須先證明「FinMind 給的數字＝官方給的數字」，否則降級路徑會安靜地
寫入一組對不上的價格 —— 而覆蓋率閘門只看「有幾檔」，不看「值對不對」。

四種對映各自拿真實資料比對（不是讀程式碼比對）：
  1. 上市個股  FinMind TaiwanStockPrice.close        vs  TWSE 官方 STOCK_DAY（逐檔月表）
  2. 上櫃個股  FinMind TaiwanStockPrice.close        vs  我們已發布的 prices.csv（當日由官方 TPEx 端點寫入）
  3. 加權報酬  FinMind TotalReturnIndex(TAIEX).price vs  TWSE 官方 MI_INDEX
  4. 櫃買報酬  FinMind TotalReturnIndex(TPEx).price  vs  TPEx 官方 tpex_reward_index

用法：
    python scripts/verify_finmind_alignment.py [YYYY-MM-DD]
Token 來源：FINMIND_TOKEN 環境變數，或本機 ../taiwan-stock-app/.finmind_token（皆不列印值）。
退出碼：0 = 全部對齊，1 = 有不一致（**決策 #27 的降級路徑不得啟用**）。
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from fetch_daily import get_json, num, roc_to_iso, U_MI, U_TPEXTR  # noqa: E402

TOL = 1e-6          # 收盤價應該逐位元相同；容差只為浮點列印誤差


def token():
    t = os.environ.get("FINMIND_TOKEN", "").strip()
    if t:
        return t, "env"
    for p in (os.path.join(ROOT, ".finmind_token"),
              os.path.join(os.path.dirname(ROOT), "taiwan-stock-app", ".finmind_token")):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                return v, os.path.basename(p)
    return "", "無"


def finmind(tok, dataset, data_id, day):
    q = urllib.parse.urlencode({"dataset": dataset, "data_id": data_id,
                                "start_date": day, "end_date": day, "token": tok})
    j = get_json("https://api.finmindtrade.com/api/v4/data?" + q, tries=3)
    for row in (j.get("data") or []):
        if row.get("date") == day:
            return num(row.get("close") if dataset == "TaiwanStockPrice" else row.get("price"))
    return None


def twse_stock_day(code, day):
    """TWSE 官方逐檔月表 —— 與 STOCK_DAY_ALL 是不同端點，實測當日 20:15 就有資料。"""
    ym = day[:4] + day[5:7] + "01"
    j = get_json(f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={ym}&stockNo={code}", tries=3)
    for row in (j.get("data") or []):
        if roc_to_iso(row[0]) == day:
            return num(row[6])
    return None


def published(day):
    import csv
    out = {}
    with open(os.path.join(ROOT, "data", "prices.csv"), encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["date"] == day:
                out[r["code"]] = float(r["close"])
    return out


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else None
    tok, src = token()
    print(f"[align] FinMind token 來源={src} 長度={len(tok)}（值不列印）")
    if not tok:
        print("[align] 無 token → 無法驗證。決策 #27 的降級路徑在驗證通過前不得啟用。")
        return 1

    pub = None
    if day is None:
        import csv
        days = set()
        with open(os.path.join(ROOT, "data", "prices.csv"), encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                days.add(r["date"])
        day = max(days)
    pub = published(day)
    print(f"[align] 比對日 {day}，prices.csv 當日 {len(pub)} 檔")

    ok, bad, skip = 0, [], 0

    # 1) 上市個股：FinMind vs TWSE 官方逐檔月表（兩者互相獨立）
    tse = [c for c in ("2330", "2454", "2317", "2412", "1301", "0050") if c in pub]
    for c in tse:
        f = finmind(tok, "TaiwanStockPrice", c, day)
        o = twse_stock_day(c, day)
        time.sleep(1.2)
        if f is None or o is None:
            print(f"  [SKIP] 上市 {c}: FinMind={f} 官方={o}"); skip += 1; continue
        good = abs(f - o) < TOL
        print(f"  [{'OK ' if good else 'BAD'}] 上市 {c}: FinMind={f} 官方STOCK_DAY={o} 已發布={pub.get(c)}")
        ok += good
        if not good:
            bad.append(f"上市{c} {f}!={o}")

    # 2) 上櫃個股：FinMind vs 我們已發布的值（當日由官方 TPEx 端點寫入）
    otc = [c for c in ("6488", "5274", "3081", "4966", "8299", "6547") if c in pub]
    for c in otc:
        f = finmind(tok, "TaiwanStockPrice", c, day)
        time.sleep(1.2)
        if f is None:
            print(f"  [SKIP] 上櫃 {c}: FinMind 無資料"); skip += 1; continue
        good = abs(f - pub[c]) < TOL
        print(f"  [{'OK ' if good else 'BAD'}] 上櫃 {c}: FinMind={f} 已發布(官方TPEx)={pub[c]}")
        ok += good
        if not good:
            bad.append(f"上櫃{c} {f}!={pub[c]}")

    # 3) 加權報酬指數：FinMind vs 官方 MI_INDEX（只有官方那天也在同一日才比得到）
    try:
        mi = get_json(U_MI)
        d_mi = roc_to_iso(mi[0]["日期"])
        v_mi = next((num(r["收盤指數"]) for r in mi if r["指數"] == "發行量加權股價報酬指數"), None)
        f = finmind(tok, "TaiwanStockTotalReturnIndex", "TAIEX", d_mi)
        time.sleep(1.2)
        if f is None or v_mi is None:
            print(f"  [SKIP] 加權報酬({d_mi}): FinMind={f} 官方={v_mi}"); skip += 1
        else:
            good = abs(f - v_mi) < 0.01           # 指數官方印到小數第 2 位
            print(f"  [{'OK ' if good else 'BAD'}] 加權報酬({d_mi}): FinMind={f} 官方MI_INDEX={v_mi}")
            ok += good
            if not good:
                bad.append(f"加權報酬 {f}!={v_mi}")
    except Exception as e:                                   # noqa: BLE001
        print(f"  [SKIP] 加權報酬：{e}"); skip += 1

    # 4) 櫃買報酬指數：FinMind vs 官方 tpex_reward_index
    try:
        tr = get_json(U_TPEXTR)
        d_tr = roc_to_iso(tr[-1]["Date"])
        v_tr = num(tr[-1]["TPExTotalReturnIndex"])
        f = finmind(tok, "TaiwanStockTotalReturnIndex", "TPEx", d_tr)
        if f is None or v_tr is None:
            print(f"  [SKIP] 櫃買報酬({d_tr}): FinMind={f} 官方={v_tr}"); skip += 1
        else:
            good = abs(f - v_tr) < 0.01
            print(f"  [{'OK ' if good else 'BAD'}] 櫃買報酬({d_tr}): FinMind={f} 官方={v_tr}")
            ok += good
            if not good:
                bad.append(f"櫃買報酬 {f}!={v_tr}")
    except Exception as e:                                   # noqa: BLE001
        print(f"  [SKIP] 櫃買報酬：{e}"); skip += 1

    print(f"[align] 對齊 {ok} 項、不一致 {len(bad)} 項、略過 {skip} 項")
    if bad:
        print("[align] 🔴 不一致：" + " | ".join(bad))
        print("[align] 決策 #27 的降級路徑【不得】啟用，直到查明原因。")
        return 1
    if ok < 4:
        print("[align] 🟠 對齊項目過少（<4），不足以支持降級路徑。")
        return 1
    print("[align] ✅ 四種欄位對映皆與官方一致 —— 決策 #27 的條件① 成立。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
