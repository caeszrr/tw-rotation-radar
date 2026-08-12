#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L7 休市日曆：抓 TWSE 官方「有價證券集中交易市場開（休）市日期」，快取進 data/holidays.json。
休市日 = 成功 + 心跳、零告警（藍圖第九節 L7）。抓不到時**不阻擋**，只記錄。
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "holidays.json")
TPE = timezone(timedelta(hours=8))
URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"


def fetch():
    req = urllib.request.Request(URL, headers={"User-Agent": "tw-rotation-radar/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def to_iso(s):
    """TWSE 回傳民國 7 碼（1150101）；也容忍帶分隔符與西元 8 碼。"""
    s = str(s).strip()
    for sep in ("/", "-"):
        if sep in s:
            parts = s.split(sep)
            if len(parts) == 3:
                y = int(parts[0])
                y = y + 1911 if y < 1911 else y
                return f"{y:04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    if s.isdigit() and len(s) == 7:                 # 民國 7 碼
        return f"{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}"
    if s.isdigit() and len(s) == 8:                 # 西元 8 碼
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def main():
    today = datetime.now(TPE).strftime("%Y-%m-%d")
    dates = []
    try:
        for r in fetch():
            d = to_iso(r.get("Date") or r.get("日期") or "")
            name = (r.get("Name") or r.get("名稱") or "").strip()
            desc = (r.get("Description") or r.get("說明") or "").strip()
            if not d:
                continue
            blob = name + desc
            # 只收「真的不開市」的日子；"開始交易日"/"最後交易日" 這類是交易日，不可誤收
            closed = any(k in blob for k in ("放假", "休市", "無交易", "停止交易"))
            trading = any(k in blob for k in ("開始交易", "最後交易", "開始集中交易"))
            if closed and not trading:
                dates.append({"date": d, "name": name, "desc": desc.replace("<br>", "")})
        dates.sort(key=lambda x: x["date"])
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": today, "source": URL, "dates": dates}, f, ensure_ascii=False, indent=1)
        print(f"[holidays] 取得 {len(dates)} 筆休市日，已寫入 data/holidays.json")
    except Exception as e:                                   # noqa: BLE001
        print(f"[holidays] 取得失敗（不阻擋管線，僅記錄）：{e}")
        return 0
    hit = [d for d in dates if d["date"] == today]
    if hit:
        print(f"[holidays] 今日 {today} 為休市日：{hit[0].get('name') or hit[0].get('desc')}")
    else:
        print(f"[holidays] 今日 {today} 非官方休市日")
    return 0


if __name__ == "__main__":
    sys.exit(main())
