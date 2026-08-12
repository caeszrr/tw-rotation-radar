#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TWRRG-v1 —— 本專案唯一標準公式的精確實作。

權威來源：`RRG_台股產業輪動雷達_技術報告_雙語版.md` §0.1（CODING AUTHORITY）。
本檔【只】實作 §0.1，不得加入任何自由參數、不得「順手改良」。
公式凍結見鐵律 #5：任何修改需先寫入變更紀錄並經專案負責人同意。

2026-08-12 裁決（frozen_params #44）：benchmark 改用【報酬指數】，
理由是 §0.1 的 "same adjustment basis" 條款 —— 成分股用還原價（含息），
基準也必須含息。本檔不硬編任何基準，基準序列由呼叫端傳入。
"""

import numpy as np
import pandas as pd

# ── §0.1 常數：exact, no free parameters ──────────────────────────────
WINDOW_N = 14        # 兩個 rolling 視窗都用 14
ROC_K = 10           # ROC 回看根數
TAIL_DEFAULT = 5     # UI 尾巴預設點數（可調 3–20）
MIN_HISTORY_FLOOR = 43   # 數學下限 14+10+14+5
REFUSE_BELOW = 60        # 少於 60 交易日 → 拒繪「歷史不足」
DISPLAY_WINDOWS = (20, 60, 120, 240)   # 顯示用，不影響計算

QUADRANTS = {
    "LEADING": "領先",
    "WEAKENING": "弱化",
    "LAGGING": "落後",
    "IMPROVING": "改善",
}


def rs_ratio_momentum(sector: pd.Series, benchmark: pd.Series) -> pd.DataFrame:
    """
    TWRRG-v1 逐行實作。sector / benchmark 皆須為【還原價】基礎的收盤序列，
    且已對齊到同一組交易日索引。

        RS          = 100 * (sector_close / benchmark_close)
        SMA_RS      = RS.rolling(14).mean()
        SD_RS       = RS.rolling(14).std(ddof=1)      # 樣本標準差
        RS_Ratio    = 100 + (RS - SMA_RS) / SD_RS     # 置中於 100
        ROC         = 100 * (RS_Ratio / RS_Ratio.shift(10) - 1)
        SMA_ROC     = ROC.rolling(14).mean()
        SD_ROC      = ROC.rolling(14).std(ddof=1)
        RS_Momentum = 100 + (ROC - SMA_ROC) / SD_ROC  # 置中於 100（不是 101）
    """
    if not sector.index.equals(benchmark.index):
        raise ValueError("sector 與 benchmark 的日期索引必須完全一致")

    rs = 100.0 * (sector / benchmark)
    sma_rs = rs.rolling(WINDOW_N).mean()
    sd_rs = rs.rolling(WINDOW_N).std(ddof=1)
    rs_ratio = 100.0 + (rs - sma_rs) / sd_rs

    roc = 100.0 * (rs_ratio / rs_ratio.shift(ROC_K) - 1.0)
    sma_roc = roc.rolling(WINDOW_N).mean()
    sd_roc = roc.rolling(WINDOW_N).std(ddof=1)
    rs_momentum = 100.0 + (roc - sma_roc) / sd_roc

    return pd.DataFrame({"rs": rs, "rs_ratio": rs_ratio, "rs_momentum": rs_momentum})


def quadrant(rs_ratio: float, rs_momentum: float) -> str | None:
    """四象限歸屬。座標中心為 (100, 100)。"""
    if rs_ratio is None or rs_momentum is None:
        return None
    if isinstance(rs_ratio, float) and (np.isnan(rs_ratio) or np.isnan(rs_momentum)):
        return None
    if rs_ratio >= 100.0:
        return "LEADING" if rs_momentum >= 100.0 else "WEAKENING"
    return "IMPROVING" if rs_momentum >= 100.0 else "LAGGING"


def equal_weight_index(prices: pd.DataFrame, base: float = 100.0) -> tuple[pd.Series, pd.Series]:
    """
    族群指數 = 等權重日報酬鏈結，基期 100（§0.1）：

        Index_t = Index_{t-1} * (1 + mean(constituent daily returns_t))

    「當日缺價的成分股不計入當日平均的分母」。
    本專案採用的精確判定（Phase 2 明文記錄的解讀）：某成分股要進入第 t 日的分母，
    必須在 t 與【前一個交易日 t-1】兩天都有價；只有單邊有價者一律排除，
    絕不用跨日報酬冒充單日報酬。

    回傳 (指數序列, 每日有效成分股數)。
    """
    if prices.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    prev = prices.shift(1)
    valid = prices.notna() & prev.notna()
    rets = (prices / prev - 1.0).where(valid)

    n_valid = valid.sum(axis=1)
    mean_ret = rets.mean(axis=1, skipna=True)          # 分母 = 當日有效成分股數
    mean_ret = mean_ret.where(n_valid > 0, np.nan)

    idx = pd.Series(np.nan, index=prices.index, dtype=float)
    level = base
    started = False
    for i, d in enumerate(prices.index):
        r = mean_ret.iloc[i]
        if not started:
            # 指數自「第一個有任一成分股報價的日子」起算，基期 100
            if prices.iloc[i].notna().any():
                idx.iloc[i] = level
                started = True
            continue
        if pd.notna(r):
            level = level * (1.0 + r)
        idx.iloc[i] = level
    return idx, n_valid


def cap_weight_index(prices: pd.DataFrame, shares: dict, base: float = 100.0):
    """
    市值加權版族群指數：權重 = 前一日收盤 × 發行股數（前一日決定，避免用當日價決定當日權重）。
    缺價判定與等權版完全相同。

    ⚠️ 已知近似：本專案只有【當期】發行股數快照（TWSE t187ap03_L / TPEx Capitals，
    as_of 2026-08-11），沒有歷史股數序列。故歷史期間的權重以當期股數計算，
    增減資與新股發行造成的權重漂移未還原。此為誠實標註的已知近似，非缺陷隱藏。
    """
    if prices.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    prev = prices.shift(1)
    valid = prices.notna() & prev.notna()
    rets = (prices / prev - 1.0).where(valid)

    sh = pd.Series({c: float(shares.get(c, 0.0)) for c in prices.columns})
    mcap_prev = prev.mul(sh, axis=1).where(valid)      # 前一日市值，且僅限有效成分
    wsum = mcap_prev.sum(axis=1, skipna=True)
    weighted = (rets * mcap_prev).sum(axis=1, skipna=True)
    mean_ret = (weighted / wsum).where(wsum > 0, np.nan)

    n_valid = valid.sum(axis=1)
    idx = pd.Series(np.nan, index=prices.index, dtype=float)
    level, started = base, False
    for i in range(len(prices.index)):
        r = mean_ret.iloc[i]
        if not started:
            if prices.iloc[i].notna().any():
                idx.iloc[i] = level
                started = True
            continue
        if pd.notna(r):
            level = level * (1.0 + r)
        idx.iloc[i] = level
    return idx, n_valid


def should_refuse(n_days: int) -> bool:
    """
    拒繪規則（§0.1 / 藍圖§五）：可用歷史 < 60 交易日 → 標「歷史不足」，不畫點。
    抽出成獨立函式，讓拒繪決策本身可被單元測試，而不是只存在於管線內。
    """
    return int(n_days) < REFUSE_BELOW


def refusal_label(n_days: int) -> str | None:
    return f"歷史不足（{int(n_days)}/{REFUSE_BELOW} 交易日）" if should_refuse(n_days) else None
