# -*- coding: utf-8 -*-
"""
Macro data fetcher.
Pulls and analyzes the key macro indicators that drive sector and stock performance.
All data is free via yfinance.
"""
import sys
sys.path.insert(0, ".")

import numpy as np
import pandas as pd
from datetime import date

from data.fetcher import fetch_ohlcv


MACRO_TICKERS = {
    "HYG":     {"name": "Credit (HYG)",      "desc": "High yield bonds. Death cross = credit stress = stocks at risk."},
    "DX-Y.NYB":{"name": "Dollar (DXY)",       "desc": "US dollar index. Rising = headwind for commodities and multinationals."},
    "^TNX":    {"name": "10Y Yield (TNX)",    "desc": "10-year Treasury yield. Rising = headwind for growth/tech stocks."},
    "^IRX":    {"name": "2Y Yield (IRX)",     "desc": "Short-term rate proxy. Used for yield curve calculation."},
    "CL=F":    {"name": "Oil (WTI)",          "desc": "Crude oil price. Directly drives COP, EOG, XOM performance."},
    "HG=F":    {"name": "Copper (HG)",        "desc": "Copper futures. Called Dr. Copper — predicts economic expansion/contraction."},
    "SPY":     {"name": "Market (SPY)",       "desc": "S&P 500. Regime check for broad market backdrop."},
}

# Stock-to-macro sensitivity mapping
STOCK_MACRO_FLAGS = {
    "COP":   ["oil", "dollar"],
    "EOG":   ["oil", "dollar"],
    "XOM":   ["oil", "dollar"],
    "FCX":   ["copper", "dollar"],
    "LIN":   ["dollar"],
    "AVGO":  ["yields"],
    "AMD":   ["yields"],
    "MSFT":  ["yields"],
    "AAPL":  ["yields"],
    "GOOGL": ["yields"],
    "META":  ["yields"],
    "NVDA":  ["yields"],
    "WMT":   ["credit"],
    "KO":    ["credit"],
    "UNH":   ["credit"],
    "SPY":   ["credit"],
}


def _golden_cross(df: pd.DataFrame, short: int = 50, long: int = 200) -> int:
    if len(df) < long:
        return -1
    sma_s = df["Close"].rolling(short).mean()
    sma_l = df["Close"].rolling(long).mean()
    if sma_s.isna().iloc[-1] or sma_l.isna().iloc[-1]:
        return -1
    return 1 if float(sma_s.iloc[-1]) > float(sma_l.iloc[-1]) else 0


def _trend(df: pd.DataFrame, period: int = 20) -> str:
    if len(df) < period + 1:
        return "FLAT"
    ma = df["Close"].rolling(period).mean()
    if ma.isna().iloc[-1]:
        return "FLAT"
    return "RISING" if float(df["Close"].iloc[-1]) > float(ma.iloc[-1]) else "FALLING"


def fetch_macro_data(start: str = "2022-01-01") -> dict:
    """
    Returns a dict of macro indicators with current value, trend, and regime.
    Handles failures gracefully — missing data returns neutral values.
    """
    end   = str(date.today())
    results = {}

    for ticker, meta in MACRO_TICKERS.items():
        try:
            df = fetch_ohlcv(ticker, start, end)
            current = float(df["Close"].iloc[-1])
            regime  = _golden_cross(df)
            trend   = _trend(df)
            # 52-week high/low for context
            high_52 = float(df["Close"].tail(252).max())
            low_52  = float(df["Close"].tail(252).min())
            pct_range = (current - low_52) / (high_52 - low_52) * 100 if high_52 > low_52 else 50

            results[ticker] = {
                "name":      meta["name"],
                "desc":      meta["desc"],
                "current":   current,
                "regime":    regime,   # 1=golden cross, 0=death cross, -1=unknown
                "trend":     trend,
                "high_52":   high_52,
                "low_52":    low_52,
                "pct_range": pct_range,
                "error":     None,
            }
        except Exception as e:
            results[ticker] = {
                "name":    meta["name"],
                "desc":    meta["desc"],
                "current": None,
                "regime":  -1,
                "trend":   "UNKNOWN",
                "error":   str(e),
            }

    # Yield curve = 10Y minus short-term rate
    try:
        tnx = results.get("^TNX", {}).get("current")
        irx = results.get("^IRX", {}).get("current")
        if tnx and irx:
            curve = tnx - irx
            results["YIELD_CURVE"] = {
                "name":    "Yield Curve (10Y-2Y)",
                "current": round(curve, 2),
                "regime":  1 if curve > 0 else 0,
                "trend":   "NORMAL" if curve > 0 else "INVERTED",
                "error":   None,
            }
    except Exception:
        pass

    return results


def macro_health_score(data: dict) -> tuple[int, int]:
    """
    Returns (score, max_score).
    Each indicator contributes 1 point when bullish/supportive.
    """
    score = 0
    max_s = 6

    # +1 credit healthy
    if data.get("HYG", {}).get("regime") == 1:
        score += 1
    # +1 dollar weak (good for commodities/multinationals)
    if data.get("DX-Y.NYB", {}).get("regime") == 0:
        score += 1
    # +1 yields falling (good for stocks)
    if data.get("^TNX", {}).get("trend") == "FALLING":
        score += 1
    # +1 yield curve normal (no recession signal)
    if data.get("YIELD_CURVE", {}).get("regime") == 1:
        score += 1
    # +1 oil bullish. Long-term regime alone is not enough when crude is
    # actively falling; otherwise the macro post recommends Energy into a selloff.
    if data.get("CL=F", {}).get("regime") == 1 and data.get("CL=F", {}).get("trend") != "FALLING":
        score += 1
    # +1 copper bullish (economic expansion)
    if data.get("HG=F", {}).get("regime") == 1:
        score += 1

    return score, max_s


def sector_rotation_phase(data: dict) -> tuple[str, str, list]:
    """
    Returns (phase_name, description, best_sectors) based on macro signals.
    Uses a simplified 4-phase economic cycle model.
    """
    hyg_bull   = data.get("HYG",       {}).get("regime") == 1
    curve_ok   = data.get("YIELD_CURVE",{}).get("regime") == 1
    oil_bull   = data.get("CL=F",      {}).get("regime") == 1 and data.get("CL=F", {}).get("trend") != "FALLING"
    copper_bull= data.get("HG=F",      {}).get("regime") == 1 and data.get("HG=F", {}).get("trend") != "FALLING"
    spy_bull   = data.get("SPY",       {}).get("regime") == 1

    if hyg_bull and curve_ok and oil_bull and copper_bull:
        return (
            "MID-CYCLE EXPANSION",
            "Economy running hot. Commodities and industrials outperform.",
            ["Energy (XLE)", "Materials (XLB)", "Industrials (XLI)", "Financials (XLF)"]
        )
    elif hyg_bull and curve_ok and not oil_bull and copper_bull:
        return (
            "MIXED / DEFENSIVE ROTATION",
            "Credit is stable, but crude is falling. Do not assume Energy leadership without sector confirmation.",
            ["Healthcare (XLV)", "Utilities (XLU)", "Real Estate (XLRE)"]
        )
    elif hyg_bull and not curve_ok:
        return (
            "LATE-CYCLE SLOWDOWN",
            "Growth slowing. Shift to defensive sectors.",
            ["Healthcare (XLV)", "Consumer Staples (XLP)", "Energy (XLE)"]
        )
    elif not hyg_bull:
        return (
            "RECESSION / CONTRACTION",
            "Credit stress. Capital preservation mode. Reduce risk.",
            ["Consumer Staples (XLP)", "Utilities (XLU)", "Healthcare (XLV)", "Cash"]
        )
    else:
        return (
            "MIXED / TRANSITIONING",
            "Signals conflicted. Reduce position size, wait for clarity.",
            ["Diversify across sectors", "Reduce options exposure"]
        )


def stock_macro_warnings(tickers: list, data: dict) -> dict:
    """
    Returns per-ticker macro warnings based on current macro regime.
    """
    warnings = {}
    dollar_rising  = data.get("DX-Y.NYB", {}).get("regime") == 1
    yields_rising  = data.get("^TNX",     {}).get("trend") == "RISING"
    oil_bearish    = data.get("CL=F",     {}).get("regime") == 0
    copper_bearish = data.get("HG=F",     {}).get("regime") == 0
    credit_stress  = data.get("HYG",      {}).get("regime") == 0

    for t in tickers:
        flags = STOCK_MACRO_FLAGS.get(t.upper(), [])
        w = []
        if "dollar" in flags and dollar_rising:
            w.append("Dollar rising -- commodity headwind")
        if "yields" in flags and yields_rising:
            w.append("10Y yields rising -- growth/tech headwind")
        if "oil" in flags and oil_bearish:
            w.append("Oil bearish -- energy sector headwind")
        if "copper" in flags and copper_bearish:
            w.append("Copper bearish -- materials headwind")
        if "credit" in flags and credit_stress:
            w.append("Credit stress (HYG) -- broad market risk")
        if credit_stress:
            w.append("HYG death cross -- reduce all exposure")
        warnings[t.upper()] = w

    return warnings
