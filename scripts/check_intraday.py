#!/usr/bin/env python3
"""盤中即時層的每日事後檢查（L6）。

在日線管線跑完後執行一次，問 Worker 的 /health：今天盤中到底有沒有正常運作。

**為什麼需要這一支**：盤中即時層自己的告警有兩條路 —— Healthchecks /fail 與
Telegram 直發 —— 但兩條都需要在 Cloudflare Worker 上設 secret，而目前
`HC_PING_URL_INTRADAY` 還沒設、Worker 也沒有 Telegram token。
於是「盤中層整天沒抓到東西」或「已退回 60 秒模式」這兩件事，
除非有人剛好打開網頁看右上角，否則沒有人會知道 —— 又是一個靜默失敗。

這支跑在 GitHub Actions 裡，那裡本來就有 Telegram secret，不必新增任何憑證。

判讀規則（保守，寧可不叫也不亂叫）：
  非交易日            → 什麼都不做（本來就不該有資料）
  沒有今天的資料      → 🟠 告警
  mode == slow        → 🟠 告警（快速模式已自動降級）
  n 明顯偏少          → 🟠 告警（抓到的檔數少於預期宇宙的 80%）
  其餘                → 印一行 OK

退出碼永遠是 0：這是「觀測」，不是閘門，不該讓日線管線因此變紅。
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notify  # noqa: E402

WORKER = os.environ.get(
    "RADAR_WORKER_URL",
    "https://tw-rotation-radar-intraday.caeszrr-radar.workers.dev",
).rstrip("/")
MIN_COVERAGE = 0.80


def get(path):
    req = urllib.request.Request(
        WORKER + path,
        # workers.dev 會擋 Python 預設 User-Agent（HTTP 403）——這不是 Worker 的 bug。
        headers={"User-Agent": "Mozilla/5.0 (tw-rotation-radar ci)"},
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    try:
        h = get("/health")
    except Exception as e:                                  # noqa: BLE001
        print(f"[intraday] /health 讀取失敗：{e} —— 不告警（可能只是網路瞬斷），僅記錄")
        return 0

    today = h.get("today") or {}
    latest = h.get("latest")
    ymd = today.get("ymd")
    print(f"[intraday] today={today}  latest={latest}")

    if not today.get("trading"):
        print(f"[intraday] {ymd} 非交易日（{today.get('why')}）→ 本來就不該有盤中資料，跳過")
        return 0

    if not latest or latest.get("tpe") != ymd:
        notify.telegram(
            f"🟠 台股輪動雷達 盤中即時層異常\n{ymd} 是交易日，但盤中層今天沒有寫入任何資料。\n"
            f"最後一筆：{(latest or {}).get('tpe')} / {(latest or {}).get('ts')}\n"
            f"檢查：{WORKER}/health"
        )
        print("[intraday] 🟠 已告警：今天沒有盤中資料")
        return 0

    if latest.get("mode") == "slow":
        notify.telegram(
            f"🟠 台股輪動雷達 盤中即時層已退回 60 秒模式\n{ymd}\n"
            f"最後一筆 {latest.get('ts')}，{latest.get('n')} 檔。\n"
            f"下一個交易日會自動重試快速模式。檢查：{WORKER}/health"
        )
        print("[intraday] 🟠 已告警：退回 60 秒模式")
        return 0

    n = latest.get("n") or 0
    expect = 0
    try:
        with open(os.path.join(os.path.dirname(__file__), "..", "data", "sectors.json"), encoding="utf-8") as f:
            s = json.load(f)
        expect = len({c for m in s.get("sectors", {}).values() for c in m.get("members", [])}) + 2
    except Exception:                                       # noqa: BLE001
        expect = 0
    if expect and n < expect * MIN_COVERAGE:
        notify.telegram(
            f"🟠 台股輪動雷達 盤中即時層覆蓋率偏低\n{ymd}：只抓到 {n} 檔，預期約 {expect} 檔。\n"
            f"檢查：{WORKER}/health"
        )
        print(f"[intraday] 🟠 已告警：覆蓋率偏低 {n}/{expect}")
        return 0

    print(f"[intraday] ✅ {ymd} 盤中層正常：{n} 檔、模式 {latest.get('mode')}、"
          f"最後一筆 {latest.get('ts')}（來源 {latest.get('src')}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
