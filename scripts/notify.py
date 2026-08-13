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
  python scripts/notify.py mask                    # Secrets 自檢，永不印全值
"""
import hashlib
import json
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


def mask():
    """
    Secrets 自檢：只印「有沒有設、長度、指紋」，**永遠不印值本身，也不印任何子字串**。

    為什麼用 sha256 前 8 碼而不是「前 4 碼…後 4 碼」：後者是密鑰的真子字串，
    貼在公開 Actions 日誌裡就是洩漏一小段；GitHub 的自動遮蔽只會蓋住完整值，
    不保證蓋住片段。指紋不是密鑰的一部分，卻足以讓人確認「換過沒、是不是同一把」。

    Telegram 另外打一次 getMe：這是**真的驗證 token 有效**，不只是「有設定」。
    回傳的 bot 使用者名稱是公開資訊，可以安全印出。
    """
    def fp(v):
        return hashlib.sha256(v.encode()).hexdigest()[:8]

    rows = [
        ("FINMIND_TOKEN", "官方端點落後時的備援"),
        ("TELEGRAM_BOT_TOKEN", "失敗通知"),
        ("TELEGRAM_CHAT_ID", "失敗通知"),
        ("HC_PING_URL_DAILY", "日線沉默失敗防護"),
        ("HC_PING_URL_INTRADAY", "盤中沉默失敗防護（未部署 Worker 前可不設）"),
    ]
    print("[notify] ===== Secrets 自檢（masked，永不印全值）=====")
    ok = True
    for name, why in rows:
        v = os.environ.get(name, "").strip()
        if not v:
            print(f"[notify]   {name:22} 未設定    —— {why}")
            if name != "HC_PING_URL_INTRADAY":
                ok = False
            continue
        extra = ""
        if name == "TELEGRAM_CHAT_ID":
            extra = "  形狀=純數字 OK" if v.lstrip("-").isdigit() else "  形狀=非數字（可疑）"
        if name.startswith("HC_PING_URL"):
            extra = "  主機=hc-ping.com OK" if "hc-ping.com/" in v else "  主機非 hc-ping.com（可疑）"
        print(f"[notify]   {name:22} 已設定    長度={len(v):<4} 指紋=sha256:{fp(v)}{extra}")

    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if tok:
        try:
            req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/getMe",
                                         headers={"User-Agent": "tw-rotation-radar/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                j = json.loads(r.read().decode("utf-8"))
            b = (j or {}).get("result") or {}
            print(f"[notify]   getMe 驗證通過 → bot=@{b.get('username')} (id={b.get('id')})  "
                  f"※ 使用者名稱與 bot id 為公開資訊")
        except Exception as e:                              # noqa: BLE001
            print(f"[notify]   getMe 驗證失敗：{e} —— token 可能無效或已被撤銷")
            ok = False
    print(f"[notify] ===== 自檢結果：{'全部就緒' if ok else '有缺漏（見上）'} =====")
    return 0 if ok else 1


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
    if cmd == "mask":
        return mask()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
