#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 2 單元測試 —— 全部使用【合成資料】，不碰真實 DB。
藍圖點名的三個案例：①已知 z-score 案例 ②缺價日分母 ③除息修正日。
另加 §0.1 規格護欄（ddof=1、置中 100 非 101、k=10 為 shift 非固定基準、拒繪門檻）。

執行：python radar/test_twrrg.py
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from twrrg import (  # noqa: E402
    rs_ratio_momentum, quadrant, equal_weight_index, cap_weight_index,
    should_refuse, refusal_label,
    WINDOW_N, ROC_K, REFUSE_BELOW, MIN_HISTORY_FLOOR,
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + detail) if detail else ""))


def days(n, start="2024-01-01"):
    return pd.bdate_range(start=start, periods=n)


# ─────────────────────────────────────────────────────────────
print("\n=== ① 已知 z-score 案例 ===")
# 構造：benchmark 恆為 1.0，sector 前 13 日皆 1.0、第 14 日跳到 1.10
# → RS 序列 = [100]*13 + [110]，14 期樣本標準差與均值可手算。
n = WINDOW_N
sec = pd.Series([1.0] * (n - 1) + [1.10], index=days(n))
bmk = pd.Series([1.0] * n, index=days(n))
res = rs_ratio_momentum(sec, bmk)
rs_vals = np.array([100.0] * (n - 1) + [110.0])
exp_mean = rs_vals.mean()
exp_sd = rs_vals.std(ddof=1)
exp_ratio = 100.0 + (110.0 - exp_mean) / exp_sd
got = res["rs_ratio"].iloc[-1]
check("RS-Ratio 對上手算 z-score",
      abs(got - exp_ratio) < 1e-9,
      f"expected={exp_ratio:.12f} got={got:.12f}")
check("前 13 日 RS-Ratio 為 NaN（rolling(14) 未滿）", res["rs_ratio"].iloc[:n - 1].isna().all())

# ddof 護欄：若誤用 ddof=0（母體），結果會不同 → 必須不相等
sd0 = rs_vals.std(ddof=0)
ratio_if_ddof0 = 100.0 + (110.0 - exp_mean) / sd0
check("ddof=1（樣本）而非 ddof=0（母體）",
      abs(got - ratio_if_ddof0) > 1e-6,
      f"ddof0 會給 {ratio_if_ddof0:.6f}")

# ─────────────────────────────────────────────────────────────
print("\n=== §0.1 規格護欄 ===")
# 置中必須是 100 不是 101：sector 與 benchmark 同步變動 → RS 恆定 → SD=0 → NaN，
# 改用「最後一日等於視窗均值」的構造來驗證置中值。
m = 40
base = pd.Series(np.linspace(1.0, 1.2, m), index=days(m))
noise = pd.Series(np.tile([1.0, 1.02, 0.98, 1.01], m // 4)[:m], index=days(m))
r2 = rs_ratio_momentum(base * noise, base)
rs = 100.0 * noise
win = rs.iloc[-WINDOW_N:]
centered = 100.0 + (rs.iloc[-1] - win.mean()) / win.std(ddof=1)
check("RS-Ratio 置中於 100（非 101）",
      abs(r2["rs_ratio"].iloc[-1] - centered) < 1e-9,
      f"got={r2['rs_ratio'].iloc[-1]:.9f} expect={centered:.9f}")

# ROC 必須是 shift(10) 滾動，不是「除以序列第二個元素」的固定基準
rr = r2["rs_ratio"]
roc_manual = 100.0 * (rr / rr.shift(ROC_K) - 1.0)
roc_fixed_base = 100.0 * (rr / rr.dropna().iloc[1] - 1.0)   # RRGPy 的「除以序列第二個元素」固定基準
sma = roc_manual.rolling(WINDOW_N).mean()
sd = roc_manual.rolling(WINDOW_N).std(ddof=1)
mom_manual = 100.0 + (roc_manual - sma) / sd
check("RS-Momentum 對上手算（ROC=shift(10) 滾動）",
      abs(r2["rs_momentum"].iloc[-1] - mom_manual.iloc[-1]) < 1e-9)
check("ROC 不是 RRGPy 的固定基準版本",
      not np.allclose(roc_manual.dropna().values[-5:], roc_fixed_base.dropna().values[-5:]))

check("四象限：RS>=100 且 Mom>=100 → LEADING", quadrant(101, 101) == "LEADING")
check("四象限：RS<100 且 Mom>=100 → IMPROVING", quadrant(99, 101) == "IMPROVING")
check("四象限：RS<100 且 Mom<100 → LAGGING", quadrant(99, 99) == "LAGGING")
check("四象限：RS>=100 且 Mom<100 → WEAKENING", quadrant(101, 99) == "WEAKENING")
check("四象限：NaN → None", quadrant(float("nan"), float("nan")) is None)
check("最低歷史常數 43 = 14+10+14+5", MIN_HISTORY_FLOOR == WINDOW_N + ROC_K + WINDOW_N + 5)
check("拒繪門檻為 60 交易日", REFUSE_BELOW == 60)

# ─────────────────────────────────────────────────────────────
print("\n=== ② 缺價日分母 ===")
idx = days(4)
# A、B 全程有價；C 在第 3 天（i=2）缺價
df = pd.DataFrame({
    "A": [100.0, 110.0, 121.0, 133.1],   # 每日 +10%
    "B": [50.0, 55.0, 60.5, 66.55],      # 每日 +10%
    "C": [10.0, 12.0, np.nan, 13.0],
}, index=idx)
ew, nvalid = equal_weight_index(df)
# i=1：A +10%、B +10%、C +20% → 分母 3，平均 = 13.3333%
check("第 2 日分母 = 3（三檔都有 t 與 t-1 價）", int(nvalid.iloc[1]) == 3)
check("第 2 日指數 = 100*(1+0.1333..)",
      abs(ew.iloc[1] - 100.0 * (1 + (0.10 + 0.10 + 0.20) / 3)) < 1e-9,
      f"got={ew.iloc[1]:.9f}")
# i=2：C 當日缺價 → 排除，分母 2，平均 = 10%
check("第 3 日分母 = 2（C 當日缺價被排除）", int(nvalid.iloc[2]) == 2)
check("第 3 日指數 = 前一日 * 1.10",
      abs(ew.iloc[2] - ew.iloc[1] * 1.10) < 1e-9)
# i=3：C 有價但 t-1 缺 → 仍排除（不得用跨日報酬冒充單日報酬）
check("第 4 日分母 = 2（C 有價但前一日缺，仍排除）", int(nvalid.iloc[3]) == 2)
check("第 4 日指數 = 前一日 * 1.10（未混入 C 的跨日報酬）",
      abs(ew.iloc[3] - ew.iloc[2] * 1.10) < 1e-9)
# 全缺價日 → 指數持平不跳空
df2 = pd.DataFrame({"A": [100.0, np.nan, 110.0]}, index=days(3))
ew2, nv2 = equal_weight_index(df2)
check("整天無任何成分股有價 → 指數持平（不 NaN、不跳空）",
      abs(ew2.iloc[1] - ew2.iloc[0]) < 1e-12 and int(nv2.iloc[1]) == 0)

# ─────────────────────────────────────────────────────────────
print("\n=== ③ 除息修正日 ===")
# 原始價在除息日跳空下跌，還原價則連續。用還原價算出的指數不得出現假跌。
idx5 = days(5)
raw = pd.Series([100.0, 100.0, 95.0, 95.0, 95.0], index=idx5)     # 第 3 日除息 5 元
cash = 5.0
ratio = (100.0 - cash) / 100.0                                     # 0.95
adj = pd.Series([100.0 * ratio, 100.0 * ratio, 95.0, 95.0, 95.0], index=idx5)
ew_raw, _ = equal_weight_index(pd.DataFrame({"X": raw}))
ew_adj, _ = equal_weight_index(pd.DataFrame({"X": adj}))
check("原始價會在除息日產生 -5% 假跌",
      abs(ew_raw.iloc[2] / ew_raw.iloc[1] - 0.95) < 1e-12)
check("還原價在除息日【無】假跌（報酬為 0）",
      abs(ew_adj.iloc[2] / ew_adj.iloc[1] - 1.0) < 1e-12,
      f"adj ret={ew_adj.iloc[2] / ew_adj.iloc[1] - 1:.2e}")
check("還原比率 = (prevClose - cash)/prevClose", abs(ratio - 0.95) < 1e-15)

# ─────────────────────────────────────────────────────────────
print("\n=== 市值加權 ===")
dfc = pd.DataFrame({"BIG": [100.0, 110.0], "SMALL": [10.0, 10.0]}, index=days(2))
shares = {"BIG": 1_000_000_000, "SMALL": 1_000_000}       # BIG 市值遠大於 SMALL
cw, _ = cap_weight_index(dfc, shares)
ew3, _ = equal_weight_index(dfc)
# 等權：(10% + 0%)/2 = 5%；市值加權應極接近 BIG 的 +10%
check("等權 = 5%", abs(ew3.iloc[1] / ew3.iloc[0] - 1.05) < 1e-12)
check("市值加權 ≈ 大權值股的 +10%",
      abs(cw.iloc[1] / cw.iloc[0] - 1.10) < 1e-3,
      f"got={cw.iloc[1] / cw.iloc[0] - 1:.6f}")

# ─────────────────────────────────────────────────────────────
print("\n=== 拒繪規則（歷史不足）===")
check("59 交易日 → 拒繪", should_refuse(59))
check("60 交易日 → 放行（邊界為『含等於』）", not should_refuse(60))
check("43 交易日（數學下限）仍拒繪 —— 數學下限不等於可繪門檻", should_refuse(MIN_HISTORY_FLOOR))
check("拒繪標籤說得出實際天數", refusal_label(12) == "歷史不足（12/60 交易日）", refusal_label(12) or "")
check("達標族群無拒繪標籤", refusal_label(644) is None)

# ─────────────────────────────────────────────────────────────
print(f"\n=== 結果：{len(PASS)} passed, {len(FAIL)} failed ===")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("✅ 單元測試全綠")
