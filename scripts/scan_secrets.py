#!/usr/bin/env python3
"""commit 前的密鑰／個資掃描（鐵律 #3）。

歷史上這件事一直是「每次手動重做一遍」，而 2026-08-14 的交接文件已經寫下教訓：
**新增一種憑證，比對名單就要跟著加，否則掃描只會給出虛假的安心感。**
把它變成腳本，就是為了讓「加一項」有個明確的地方可以加。

兩種掃法，缺一不可：
  1. **樣式掃描**：找出「長得像密鑰」的東西（Telegram bot token / JWT / sk-ant- /
     AKIA / AIza / hc-ping URL / 私人 email / 本機絕對路徑 / 內網 IP / 十六進位長字串）。
     好處是連沒列進名單的新憑證也可能抓到。
  2. **逐字比對**：把本機真實持有的密鑰值直接拿去比對。
     樣式掃描抓不到「不長得像密鑰的密鑰」，這一關才抓得到。

用法：
    python scripts/scan_secrets.py                    # 掃已追蹤 + 待加入的檔案
    python scripts/scan_secrets.py --literal <值> ... # 額外加入逐字比對的值

退出碼：0 = 乾淨，1 = 有命中（**不要 commit**）。
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 1. 樣式 ────────────────────────────────────────────────────────────
PATTERNS = [
    ("Telegram bot token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
    ("JWT",                re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")),
    ("Anthropic key",      re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI key",         re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    ("AWS key id",         re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key",     re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Healthchecks URL",   re.compile(r"hc-ping\.com/[0-9a-f-]{8,}")),
    ("私人 email",         re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\.|users\.noreply\.)"
                                      r"[A-Za-z0-9.-]+\.(?:com|net|org|tw|io)\b")),
    ("本機絕對路徑",       re.compile(r"[A-Za-z]:\\\\?Users\\\\?[A-Za-z0-9_.-]+")),
    ("內網 IP",            re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
    # SELFTEST_TOKEN（2026-08-14 新增的憑證種類）與同類的裸十六進位長字串。
    # KV namespace id 也是 32 位十六進位，但那不是密鑰（見下方 ALLOW），所以要放行。
    ("長十六進位字串",     re.compile(r"\b[0-9a-f]{40,}\b")),
]

# 已知且刻意公開、不是密鑰的東西。每一條都要寫清楚為什麼可以放行。
ALLOW = [
    # Cloudflare KV namespace id：不是憑證，沒有它也照樣要通過帳號授權才動得了 KV。
    re.compile(r"\b5be6c03721a1423c9f89670c6cee1b94\b"),
    # GitHub Actions bot 的固定 email
    re.compile(r"41898282\+github-actions\[bot\]@users\.noreply\.github\.com"),
    re.compile(r"github-actions\[bot\]@users\.noreply\.github\.com"),
    # 說明文字裡示範用的路徑寫法
    re.compile(r"C:\\\\Users\\\\USER\\\\Desktop\\\\tw-rotation-radar"),
]

# ── 2. 逐字比對：本機真實持有的密鑰值 ──────────────────────────────────
# 從環境變數與已知的本機檔案讀進來（這些檔案本身都在 .gitignore 裡）。
# **新增一種憑證時，把來源加到這裡。**
LITERAL_SOURCES = [
    ("FINMIND_TOKEN", "env"), ("TELEGRAM_BOT_TOKEN", "env"), ("TELEGRAM_CHAT_ID", "env"),
    ("HC_PING_URL_DAILY", "env"), ("HC_PING_URL_INTRADAY", "env"),
    ("ANTHROPIC_API_KEY", "env"), ("GEMINI_API_KEY", "env"), ("FUGLE_API_KEY", "env"),
    (".finmind_token", "file"), (".gemini_key", "file"),
    # Cloudflare wrangler 的本機快取：OAuth token / refresh token / account id / email
    (os.path.join("worker", ".wrangler", "cache", "wrangler-account.json"), "json"),
    (os.path.join(os.path.expanduser("~"), ".wrangler", "config", "default.toml"), "raw"),
]

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".wrangler", "build"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".zip", ".db"}


def literals(extra):
    out = set(extra or [])
    for name, kind in LITERAL_SOURCES:
        try:
            if kind == "env":
                v = os.environ.get(name, "").strip()
                if v:
                    out.add(v)
            elif kind == "file":
                p = os.path.join(ROOT, name)
                if os.path.exists(p):
                    out.add(open(p, encoding="utf-8").read().strip())
            else:
                p = name if os.path.isabs(name) else os.path.join(ROOT, name)
                if os.path.exists(p):
                    txt = open(p, encoding="utf-8", errors="replace").read()
                    out.update(re.findall(r"[A-Za-z0-9_.\-@]{16,}", txt))
        except Exception:                                   # noqa: BLE001
            pass
    return {v for v in out if len(v) >= 12}


def files():
    """已追蹤的檔案 + 尚未追蹤但即將被 git add 的檔案（掃描要涵蓋「這次要 commit 的」）。"""
    out = []
    for cmd in (["git", "ls-files"], ["git", "ls-files", "--others", "--exclude-standard"]):
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        out += [l for l in r.stdout.splitlines() if l.strip()]
    keep = []
    for f in sorted(set(out)):
        if any(part in SKIP_DIRS for part in f.split("/")):
            continue
        if os.path.splitext(f)[1].lower() in SKIP_EXT:
            continue
        keep.append(f)
    return keep


def main(argv):
    extra = argv[argv.index("--literal") + 1:] if "--literal" in argv else []
    lits = literals(extra)
    fs = files()
    hits = []
    for f in fs:
        p = os.path.join(ROOT, f)
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except Exception:                                   # noqa: BLE001
            continue
        for i, line in enumerate(txt.splitlines(), 1):
            if any(a.search(line) for a in ALLOW):
                continue
            for label, pat in PATTERNS:
                m = pat.search(line)
                if m:
                    hits.append((f, i, label, m.group(0)[:12] + "…"))
            for v in lits:
                if v and v in line:
                    hits.append((f, i, "★逐字命中真實密鑰", "(不印值)"))

    print(f"[scan] 掃描 {len(fs)} 檔 × {len(PATTERNS)} 類樣式 + {len(lits)} 組逐字比對值")
    if not hits:
        print("[scan] ✅ 0 命中")
        return 0
    print(f"[scan] 🔴 {len(hits)} 命中：")
    for f, i, label, sample in hits:
        print(f"   {f}:{i}  [{label}]  {sample}")
    print("[scan] 不要 commit，先處理上列項目（或把確定安全的加進 ALLOW 並寫明理由）")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
