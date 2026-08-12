#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
管線 P2 —— 族群指數 → TWRRG-v1 → docs/history.json（全歷史持久化）。

與主專案版本的唯一差異：資料來源由 SQLite 改為 data/prices.csv（四欄種子＋每日增量）。
計算邏輯逐行相同，公式凍結見鐵律 #5。

執行：python scripts/build_history.py
"""
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from twrrg import (  # noqa: E402
    rs_ratio_momentum, quadrant, equal_weight_index, cap_weight_index,
    REFUSE_BELOW, WINDOW_N, ROC_K, TAIL_DEFAULT, DISPLAY_WINDOWS,
)

PRICES = os.path.join(ROOT, "data", "prices.csv")
START_FLOOR = "2023-12-12"
BENCH_TAIEX, BENCH_TPEX = "TAIEX_TR", "TPEX_TR"
WEIGHTINGS = ["equal", "cap"]
BENCHMARKS = ["sector_avg", "taiex", "tpex", "combined"]
BENCH_LABEL = {"sector_avg": "全部族群等權平均", "taiex": "發行量加權股價報酬指數",
               "tpex": "櫃買報酬指數", "combined": "上市+上櫃市值併權"}
SIZE_ITEMS = [("上市大型", "0050"), ("上市中型", "0051"), ("上櫃大型", "006201"),
              ("高股息", "0056"), ("低波高息", "00713"), ("公司治理", "00692"),
              ("上櫃中小", None)]
SIZE_PENDING_LABEL = "資料累積中（富櫃200指數無歷史來源，逐日累積，滿 60 交易日前不畫點）"


def log(m):
    print(m, flush=True)


def load():
    with open(os.path.join(ROOT, "data", "sectors.json"), encoding="utf-8") as f:
        sectors = json.load(f)
    with open(os.path.join(ROOT, "data", "shares_snapshot.json"), encoding="utf-8") as f:
        snap = json.load(f)

    by_code = defaultdict(dict)
    with open(PRICES, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["date"] < START_FLOOR:
                continue
            try:
                v = float(r["adj_close"])
            except (TypeError, ValueError):
                continue
            if v > 0:                                   # 護欄：非正還原價一律視為缺價
                by_code[r["code"]][r["date"]] = v

    cal = sorted(by_code.get(BENCH_TAIEX, {}).keys())   # 交易日曆以加權報酬指數為準
    prices = pd.DataFrame({c: pd.Series(d) for c, d in by_code.items()}).reindex(cal)
    bench = prices[[BENCH_TAIEX, BENCH_TPEX]]
    return sectors, snap, cal, prices, bench


def chain(returns, base=100.0):
    out, lvl = [], base
    for r in returns:
        if pd.notna(r):
            lvl *= (1.0 + r)
        out.append(lvl)
    return pd.Series(out, index=returns.index, dtype=float)


def main():
    t0 = datetime.now()
    sectors, snap, cal, prices, bench = load()
    shares = snap["shares"]
    w_tw = snap["board_mktcap"]["上市權重"]
    w_ot = snap["board_mktcap"]["上櫃權重"]
    log(f"交易日曆：{len(cal)} 天　{cal[0]} .. {cal[-1]}")
    log(f"價格矩陣：{prices.shape[1]} 檔 × {prices.shape[0]} 天")

    sec_idx = {w: {} for w in WEIGHTINGS}
    sec_meta = {}
    for name, members in sectors["sectors"].items():
        cols = [c for c in members if c in prices.columns]
        sub = prices[cols]
        ew, nval = equal_weight_index(sub)
        cw, _ = cap_weight_index(sub, shares)
        sec_idx["equal"][name], sec_idx["cap"][name] = ew, cw
        n_days = int(ew.notna().sum())
        sec_meta[name] = {
            "members": members, "members_with_data": len(cols),
            "member_names": {c: (snap["meta"].get(c, {}).get("name") or c) for c in members},
            "member_markets": {c: (snap["meta"].get(c, {}).get("market") or "") for c in members},
            "index_days": n_days, "insufficient_history": bool(n_days < REFUSE_BELOW),
            "min_valid_members": int(nval.min()), "max_valid_members": int(nval.max()),
        }
        flag = "  *** 歷史不足，拒繪 ***" if n_days < REFUSE_BELOW else ""
        log(f"  {name:<16} 成分 {len(cols):>2}/{len(members):<2} 指數 {n_days} 天{flag}")

    benches = {}
    tw_ret = bench[BENCH_TAIEX] / bench[BENCH_TAIEX].shift(1) - 1.0
    ot_ret = bench[BENCH_TPEX] / bench[BENCH_TPEX].shift(1) - 1.0
    for w in WEIGHTINGS:
        df = pd.DataFrame(sec_idx[w])
        benches[(w, "sector_avg")] = equal_weight_index(df)[0]
        benches[(w, "taiex")] = bench[BENCH_TAIEX]
        benches[(w, "tpex")] = bench[BENCH_TPEX]
        benches[(w, "combined")] = chain(w_tw * tw_ret + w_ot * ot_ret)

    def rrg_block(idx_map, meta, bser):
        out, tr = {}, []
        for name in meta:
            if meta[name]["insufficient_history"]:
                out[name] = {"insufficient_history": True,
                             "pending_reason": meta[name].get("pending_reason")}
                continue
            r = rs_ratio_momentum(idx_map[name], bser)
            rr, mm = r["rs_ratio"].astype(float), r["rs_momentum"].astype(float)
            q = [quadrant(a, c) for a, c in zip(rr.values, mm.values)]
            out[name] = {"rs_ratio": [None if pd.isna(v) else round(float(v), 4) for v in rr],
                         "rs_momentum": [None if pd.isna(v) else round(float(v), 4) for v in mm],
                         "quadrant": q}
            prev = None
            for i, cur in enumerate(q):
                if cur is not None and prev is not None and cur != prev:
                    tr.append({"date": cal[i], "sector": name, "from": prev, "to": cur})
                if cur is not None:
                    prev = cur
        tr.sort(key=lambda x: (x["date"], x["sector"]))
        return out, tr

    combos, transitions = {}, {}
    for w in WEIGHTINGS:
        for b in BENCHMARKS:
            combos[f"{w}|{b}"], transitions[f"{w}|{b}"] = rrg_block(sec_idx[w], sec_meta, benches[(w, b)])

    size_idx, size_meta = {}, {}
    for label, code in SIZE_ITEMS:
        if code is None or code not in prices.columns:
            size_meta[label] = {"code": code, "name": "富櫃200指數" if code is None else code,
                                "index_days": 0, "insufficient_history": True,
                                "pending_reason": SIZE_PENDING_LABEL}
            continue
        s, _ = equal_weight_index(prices[[code]])
        size_idx[label] = s
        n = int(s.notna().sum())
        size_meta[label] = {"code": code, "name": snap["meta"].get(code, {}).get("name", code),
                            "index_days": n, "insufficient_history": bool(n < REFUSE_BELOW)}
    size_df = pd.DataFrame(size_idx)
    size_combos, size_trans = {}, {}
    for w in WEIGHTINGS:
        for b in BENCHMARKS:
            bser = equal_weight_index(size_df)[0] if b == "sector_avg" else benches[(w, b)]
            size_combos[f"{w}|{b}"], size_trans[f"{w}|{b}"] = rrg_block(size_idx, size_meta, bser)

    first_valid, last_valid = {}, {}
    for k, secs in combos.items():
        fv = lv = None
        for d in secs.values():
            if d.get("insufficient_history"):
                continue
            ok = [i for i, (a, b) in enumerate(zip(d["rs_ratio"], d["rs_momentum"]))
                  if a is not None and b is not None]
            if not ok:
                continue
            fv = cal[ok[0]] if fv is None else min(fv, cal[ok[0]])
            lv = cal[ok[-1]] if lv is None else max(lv, cal[ok[-1]])
        first_valid[k], last_valid[k] = fv, lv

    out = {
        "meta": {
            "spec": "TWRRG-v1",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "sectors_version": sectors["version"],
            "anchors": sectors.get("anchors", []),
            "size_pending_label": SIZE_PENDING_LABEL,
            "params": {"window_n": WINDOW_N, "roc_k": ROC_K, "tail_default": TAIL_DEFAULT,
                       "display_windows": list(DISPLAY_WINDOWS), "refuse_below": REFUSE_BELOW},
            "index_start_floor": START_FLOOR,
            "benchmark_basis": "total_return",
            "benchmark_labels": BENCH_LABEL,
            "combined_weights": {"上市": w_tw, "上櫃": w_ot},
            "shares_snapshot_as_of": snap["as_of"],
            "known_approximation": "市值權重使用當期股數快照，歷史增減資未還原；還原價僅處理現金股利",
            "intraday_note": "history.json 只含官方收盤；盤中暫定值永不寫入",
            "first_valid_date": first_valid, "last_valid_date": last_valid,
        },
        "dates": cal, "sectors": sec_meta, "combos": combos, "transitions": transitions,
        "size_items": size_meta, "size_combos": size_combos, "size_transitions": size_trans,
    }
    path = os.path.join(ROOT, "docs", "history.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log(f"\ndocs/history.json：{os.path.getsize(path)/1e6:.2f} MB　組合 {len(combos)}　族群 {len(sec_meta)}"
        f"　規模 {len(size_meta)}　交易日 {len(cal)}")
    log(f"首個可繪日 {first_valid['equal|taiex']}　最後可繪日 {last_valid['equal|taiex']}")
    log(f"耗時 {(datetime.now()-t0).total_seconds():.1f}s")


if __name__ == "__main__":
    main()
