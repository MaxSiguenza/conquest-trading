# -*- coding: utf-8 -*-
"""
Morning Intelligence Brief — Data Collection Layer
===================================================
Collects real-time market data, FRED macro data, and signal data,
then calls Conquest Brain to generate a full AI analyst brief.

Data sources:
  - yfinance:      SPY/QQQ/DIA, VIX, yields, dollar, gold, crude, copper, HYG, sector ETFs
  - FRED API:      GDP, CPI, Fed Funds, yield curve, unemployment, sentiment
  - alerts.scanner: watchlist scan signals
  - paper_trader:  running paper trade statistics

Cache: morning_brief.json  (regenerated once per trading day)
"""

import os
import sys
import json
from datetime import datetime, timezone, date as _date

APP_DIR    = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(APP_DIR, "morning_brief.json")
sys.path.insert(0, APP_DIR)


# ── Market tickers to track ───────────────────────────────────────────────────

CORE_TICKERS = {
    "SPY":     "S&P 500",
    "QQQ":     "Nasdaq 100",
    "DIA":     "Dow Jones",
    "^VIX":    "VIX",
    "^TNX":    "10Y Yield",
    "^IRX":    "2Y Yield",
    "UUP":     "US Dollar",
    "GLD":     "Gold",
    "CL=F":    "WTI Crude",
    "HG=F":    "Copper",
    "HYG":     "HYG Credit",
}

SECTOR_TICKERS = {
    "XLK":  "Tech",
    "XLE":  "Energy",
    "XLF":  "Financials",
    "XLV":  "Healthcare",
    "XLI":  "Industrials",
    "XLC":  "Comm Services",
    "XLY":  "Cons Disc",
    "XLP":  "Cons Staples",
    "XLRE": "Real Estate",
    "XLU":  "Utilities",
    "XLB":  "Materials",
}


# ── Data fetchers ─────────────────────────────────────────────────────────────

def _fetch_market_snapshot() -> dict:
    """Pull prices and changes for all core + sector tickers via yfinance."""
    import yfinance as yf
    import pandas as pd

    all_tickers = list(CORE_TICKERS.keys()) + list(SECTOR_TICKERS.keys())
    all_names   = {**CORE_TICKERS, **SECTOR_TICKERS}
    snapshot    = {}

    try:
        data = yf.download(
            " ".join(all_tickers),
            period="6d",
            progress=False,
            auto_adjust=True,
        )

        # yfinance returns MultiIndex columns when multiple tickers are requested
        if isinstance(data.columns, pd.MultiIndex):
            closes = data["Close"]
        else:
            # Single ticker fallback (shouldn't happen)
            closes = data[["Close"]].rename(columns={"Close": all_tickers[0]})

        for ticker, name in all_names.items():
            try:
                series = closes[ticker].dropna()
                if len(series) < 2:
                    continue
                price = float(series.iloc[-1])
                prev  = float(series.iloc[-2])
                open5 = float(series.iloc[0])
                chg   = (price / prev - 1) * 100
                ret5  = (price / open5 - 1) * 100 if len(series) >= 2 else chg
                snapshot[ticker] = {
                    "name":  name,
                    "price": round(price, 2),
                    "chg":   round(chg, 3),    # 1-day change %
                    "ret5":  round(ret5, 3),   # 5-day change %
                }
            except Exception:
                pass

    except Exception:
        pass  # snapshot stays empty — brief will note data unavailability

    return snapshot


def _sector_rotation(snapshot: dict) -> list:
    """Return sector ETFs sorted by 5-day return (best → worst)."""
    ranked = []
    for ticker, name in SECTOR_TICKERS.items():
        if ticker in snapshot:
            d = snapshot[ticker]
            ranked.append({
                "ticker": ticker,
                "name":   name,
                "chg":    d["chg"],
                "ret5":   d["ret5"],
            })
    ranked.sort(key=lambda x: -x["ret5"])
    return ranked


# ── Main data collection ──────────────────────────────────────────────────────

def collect_brief_data(watchlist: list = None) -> dict:
    """
    Collect all data needed for the brief.
    Each source is independent — failures are logged but don't abort the rest.
    """
    result = {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "market_date":     str(_date.today()),
        "snapshot":        {},
        "sector_rotation": [],
        "fred":            {},
        "scan":            [],
        "paper_stats":     {},
        "errors":          [],
    }

    # 1. Live market prices
    try:
        result["snapshot"]        = _fetch_market_snapshot()
        result["sector_rotation"] = _sector_rotation(result["snapshot"])
    except Exception as e:
        result["errors"].append(f"Market data: {e}")

    # 2. FRED macro indicators
    try:
        from macro.fred_data import fetch_fred_macro
        result["fred"] = fetch_fred_macro()
    except Exception as e:
        result["errors"].append(f"FRED: {e}")

    # 3. Watchlist signal scan
    try:
        if watchlist:
            from alerts.scanner import scan_watchlist
            result["scan"] = scan_watchlist(watchlist)
    except Exception as e:
        result["errors"].append(f"Signal scan: {e}")

    # 4. Paper trading stats
    try:
        from paper_trader import get_paper_stats
        result["paper_stats"] = get_paper_stats()
    except Exception as e:
        result["errors"].append(f"Paper stats: {e}")

    return result


# ── Cache helpers ─────────────────────────────────────────────────────────────

def load_cached_brief() -> dict:
    """Return today's cached brief dict, or {} if stale/missing."""
    try:
        with open(CACHE_FILE) as f:
            cached = json.load(f)
        if cached.get("market_date") == str(_date.today()) and cached.get("sections"):
            return cached
    except Exception:
        pass
    return {}


def _save_cache(brief: dict):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(brief, f, indent=2)
    except Exception:
        pass


# ── Public entry point ────────────────────────────────────────────────────────

def generate_brief(watchlist: list = None, force: bool = False) -> dict:
    """
    Generate (or return cached) Morning Intelligence Brief.

    Args:
        watchlist: list of ticker strings for signal scan
        force:     if True, regenerate even if today's cache exists

    Returns:
        {
          "market_date":     "2026-05-21",
          "generated_at":    "2026-05-21T13:45:00+00:00",
          "sections": {
              "macro_regime":          "...",
              "overnight":             "...",
              "data_vs_consensus":     "...",
              "sector_positioning":    "...",
              "portfolio_implications":"...",
              "what_to_watch":         "..."
          },
          "discord_summary":  "...",    # short Discord-friendly version
          "snapshot":         {...},
          "sector_rotation":  [...],    # sorted best → worst
          "fred":             {...},
          "errors":           [...]
        }
    """
    if not force:
        cached = load_cached_brief()
        if cached:
            return cached

    data = collect_brief_data(watchlist=watchlist)

    try:
        from conquest_brain import intelligence_brief
        sections, discord_summary = intelligence_brief(data)
    except Exception as e:
        sections        = {"error": str(e)}
        discord_summary = f"Brief generation failed: {e}"

    brief = {
        "market_date":     data["market_date"],
        "generated_at":    data["generated_at"],
        "sections":        sections,
        "discord_summary": discord_summary,
        "snapshot":        data["snapshot"],
        "sector_rotation": data["sector_rotation"],
        "fred":            data["fred"],
        "errors":          data["errors"],
    }

    _save_cache(brief)
    return brief
