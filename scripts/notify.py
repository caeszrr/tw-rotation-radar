#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4 心跳 + L5 雙重告警。所有密鑰只從環境變數讀（鐵律 #3），未設定時**優雅跳過**。

  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   → 失敗路徑通知
  HC_PING_URL_DAILY                       → 日線成功心跳（Period 1天 / Grace 2小時）
  HC_PING_URL_INTRADAY                    → 盤中心跳（Grace 15 分鐘）

用法：
  python scripts/notify.py fail  <run_id> <步驟> <錯誤訊息>
  python scripts/notify.py ok    <run_id> <訊息>
  python scripts/notify.py ping  daily|intraday [/fail]
"""
import os
import sys
import urllib.parse
import urllib.request


def _post(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "tw-rotation-radar/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def telegram(text):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not tok or not chat:
        print("[notify] TELEGRAM_BOT_TOKEN/CHAT_ID 未設定 → 跳過 Telegram 通知")
        return False
    body = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    try:
        _post(f"https://api.telegram.org/bot{tok}/sendMessage", body)
        print("[notify] Telegram 已送出")
        return True
    except Exception as e:                                  # noqa: BLE001
        print(f"[notify] Telegram 送出失敗：{e}")
        return False


def ping(which, suffix=""):
    key = "HC_PING_URL_DAILY" if which == "daily" else "HC_PING_URL_INTRADAY"
    url = os.environ.get(key, "").strip()
    if not url:
        print(f"[notify] {key} 未設定 → 跳過心跳（沉默失敗防護尚未生效）")
        return False
    try:
        _post(url.rstrip("/") + suffix)
        print(f"[notify] 心跳已送出 {which}{suffix}")
        return True
    except Exception as e:                                  # noqa: BLE001
        print(f"[notify] 心跳送出失敗：{e}")
        return False


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "fail":
        run_id, step, err = (argv + ["", "", ""])[2:5]
        telegram(f"🔴 台股輪動雷達 失敗\nrun={run_id}\n步驟：{step}\n{err[:600]}")
        ping("daily", "/fail")
        return 0
    if cmd == "ok":
        run_id, msg = (argv + ["", ""])[2:4]
        telegram(f"🟢 台股輪動雷達 {run_id}\n{msg[:600]}")
        return 0
    if cmd == "ping":
        which = argv[2] if len(argv) > 2 else "daily"
        suffix = argv[3] if len(argv) > 3 else ""
        ping(which, suffix)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
