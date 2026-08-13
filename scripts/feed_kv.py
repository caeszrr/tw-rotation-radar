#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KV 餵料：把 Worker 開工所需的兩把鑰匙寫進 Cloudflare KV。

**為什麼需要這支**（2026-08-13 補洞）：
`worker/src/index.js` 的 `tick()` 一開頭就讀 `KV.universe`（要抓哪些檔）與
`KV.holidays`（今天休不休市）。夜間長跑把「讀」寫好了，卻**從頭到尾沒寫過「寫」**
—— 部署後每分鐘只會回 `{error:'KV 缺少 universe'}`，一筆報價都抓不到，
而且因為 Worker 沒有崩潰，看起來像正常運轉。

**設計紀律**
- **可重跑**：KV put 是覆蓋語意，重跑同一天結果完全相同；不累積、不追加。
- **來源單一**：universe 出自 `data/sectors.json`（Phase 1 簽版 223 檔）+ 規模分頁 ETF；
  holidays 出自 `data/holidays.json`（`scripts/holidays.py` 抓 TWSE 官方表）。
  **不在這裡新增任何成分股** —— 這支只做搬運，不做裁決。
- **市場別用量的，不用猜**：上市/上櫃決定 MIS 的 `tse_` / `otc_` 前綴，猜錯就抓不到。
  故實際打兩支官方 OpenAPI 清單來分類（零 token、零費用），分不出來的**當場報錯中止**，
  絕不預設某一邊 —— 靜默塞錯前綴會變成「那檔永遠沒報價」的沉默失敗。

用法：
    python scripts/feed_kv.py --dry-run      # 只產 build/kv_*.json，不碰 CF（無帳號也能驗）
    python scripts/feed_kv.py                # 產檔後執行 wrangler 寫入 KV（需先 wrangler login）

前置：`cd worker && npx wrangler login`，且 `worker/wrangler.toml` 的 KV id 已填。
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "worker")
BUILD = os.path.join(ROOT, "build")

U_TWSE = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
U_TPEX = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

# 規模分頁 ETF（與 scripts/fetch_daily.py 的 expected_universe() 同一組，改一邊就會對不上）
SIZE_ETFS = ["0050", "0051", "006201", "0056", "00713", "00692"]
# MIS 指數頻道：t00=加權指數、o00=櫃買指數
INDEX_CH = ["tse_t00.tw", "otc_o00.tw"]


def log(tag, msg):
    print(f"[feed_kv] {tag}: {msg}")


def get_json(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": "tw-rotation-radar/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def load(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return json.load(f)


def build_universe():
    sec = load("data/sectors.json")
    members = sorted({c for v in sec["sectors"].values() for c in v})
    codes = members + [c for c in SIZE_ETFS if c not in members]
    log("L1-src", f"sectors.json v{sec['version']}：{len(sec['sectors'])} 族群 / "
                  f"{len(members)} 檔成分股 + {len(SIZE_ETFS)} 檔規模 ETF = {len(codes)} 檔")

    log("L1-fetch", "打官方清單分類上市/上櫃（零 token）…")
    twse = {str(r.get("Code") or r.get("證券代號") or "").strip() for r in get_json(U_TWSE)}
    tpex = {str(r.get("SecuritiesCompanyCode") or r.get("Code") or "").strip() for r in get_json(U_TPEX)}
    log("L1-fetch", f"TWSE {len(twse)} 檔　TPEx {len(tpex)} 檔")

    ch, market, unknown, both = [], {}, [], []
    for c in codes:
        in_t, in_o = c in twse, c in tpex
        if in_t and in_o:
            both.append(c)            # 理論上不該發生；發生就必須人看，不能自己選一邊
        if in_t:
            ch.append(f"tse_{c}.tw"); market[c] = "tse"
        elif in_o:
            ch.append(f"otc_{c}.tw"); market[c] = "otc"
        else:
            unknown.append(c)

    if both:
        log("L1-FAIL", f"同時出現在兩個市場清單，需人工裁決：{both}")
        sys.exit(4)
    if unknown:
        log("L1-FAIL", f"{len(unknown)} 檔在兩份官方清單都找不到 → 中止（絕不預設前綴）：{unknown}")
        sys.exit(3)

    ch += INDEX_CH
    payload = {
        "version": sec["version"],
        "spec": sec.get("spec", "TWRRG-v1"),
        "n_stocks": len(codes),
        "n_ch": len(ch),
        "ch": ch,
        "market": market,
        "note": "指數 t00/o00 為【價格】指數；日線基準用的是【報酬】指數。"
                "盤中只取當日相對漂移（p/prev-1），兩邊皆為價格基礎故一致，"
                "但這是刻意的近似，不可用來取代收盤定稿。",
    }
    return payload


def build_holidays():
    hol = load("data/holidays.json")
    n = len(hol.get("dates", []))
    log("L1-src", f"holidays.json：{n} 筆休市日（fetched_at={hol.get('fetched_at')}）")
    if n == 0:
        log("L1-warn", "休市日 0 筆 —— Worker 將只擋週末（已知降級，非靜默）")
    # 原樣送出物件陣列：worker 的 holidaySet() 已相容 {date,...}，
    # 這裡刻意【不】先轉成字串陣列，讓真實格式一路走到底、少一層可以說謊的轉換。
    return hol


def wrangler_put(key, path):
    """新舊兩套 wrangler 子指令都試（v3 是 kv:key，v4 是 kv key）。"""
    variants = [
        ["npx", "wrangler", "kv", "key", "put", key, f"--path={path}", "--binding=QUOTES", "--remote"],
        ["npx", "wrangler", "kv:key", "put", key, f"--path={path}", "--binding=QUOTES"],
    ]
    for cmd in variants:
        log("L5-put", " ".join(cmd))
        # 明指 utf-8：Windows 預設用 cp950 解 wrangler 的輸出會炸 UnicodeDecodeError，
        # 而那個例外會蓋掉真正的錯誤訊息（2026-08-14 首次實跑踩到，當時 put 其實成功了，
        # 但畫面上先跳一個看起來很嚴重的 traceback）。
        p = subprocess.run(cmd, cwd=WORKER, shell=(os.name == "nt"),
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode == 0:
            log("L5-ok", f"{key} 寫入成功")
            return True
        log("L5-retry", f"退出碼 {p.returncode}：{out.strip().splitlines()[-1] if out.strip() else '(無輸出)'}")
    log("L5-FAIL", f"{key} 寫入失敗 —— 請確認已 wrangler login 且 wrangler.toml 的 KV id 已填")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只產檔，不呼叫 wrangler")
    args = ap.parse_args()

    os.makedirs(BUILD, exist_ok=True)
    jobs = [("universe", build_universe()), ("holidays", build_holidays())]

    paths = {}
    for key, payload in jobs:
        p = os.path.join(BUILD, f"kv_{key}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        paths[key] = p
        log("L2-built", f"{key} → {os.path.relpath(p, ROOT)}　{os.path.getsize(p):,} bytes")

    u = jobs[0][1]
    log("L3-check", f"ex_ch 共 {u['n_ch']} 筆（{u['n_stocks']} 檔個股/ETF + {len(INDEX_CH)} 檔指數）")
    log("L3-check", f"前 3 筆：{u['ch'][:3]}　後 2 筆：{u['ch'][-2:]}")

    if args.dry_run:
        log("L9-done", "--dry-run：未呼叫 wrangler。授權後執行 `python scripts/feed_kv.py` 即寫入 KV。")
        return 0

    ok = all(wrangler_put(k, paths[k]) for k, _ in jobs)
    if not ok:
        return 1
    log("L9-done", "KV 餵料完成。可用 `curl <worker>/health` 與 `/latest` 驗證。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
