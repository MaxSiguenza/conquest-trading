# -*- coding: utf-8 -*-
"""
Conquest Scan Universe — Stock Buying Only
==========================================
This universe is used for AUTO-DISCOVERY of stocks to buy (long equity).
NOT for options trading — options tools are separate (Spread Finder, CSP, etc.)

Every morning at 10 AM the system scans all tickers below. Any that fire a
real entry signal AND pass the stock-quality filters get a full AI thesis
posted to #watchlist automatically. You do nothing.

To customise: edit UNIVERSE freely. Keep it under ~120 tickers.
Tickers in EXCLUDE_FROM_THESIS are scanned but never get a thesis card.
"""

UNIVERSE = [
    # ── Mega-cap tech ──────────────────────────────────────────────────────
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "ORCL",
    # ── Semiconductors ─────────────────────────────────────────────────────
    "AMD", "AVGO", "QCOM", "MU", "AMAT", "LRCX", "KLAC", "MRVL", "ON",
    # ── Cloud / Software / AI ──────────────────────────────────────────────
    "CRM", "ADBE", "NOW", "SNOW", "PLTR", "DDOG", "CRWD", "ZS", "NET",
    "PANW", "TTD", "HUBS", "MDB", "GTLB",
    # ── Financials ─────────────────────────────────────────────────────────
    "JPM", "GS", "BAC", "MS", "WFC", "V", "MA", "AXP", "PYPL", "COF",
    # ── Healthcare / Biotech ───────────────────────────────────────────────
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "PFE", "AMGN", "GILD",
    "MRNA", "REGN", "VRTX", "BSX", "ISRG", "TMO", "DHR",
    # ── Energy ─────────────────────────────────────────────────────────────
    "XOM", "CVX", "COP", "SLB", "OXY", "MPC", "PSX",
    # ── Consumer Discretionary ─────────────────────────────────────────────
    "WMT", "HD", "MCD", "SBUX", "NKE", "COST", "TGT", "LULU", "CMG", "BKNG",
    # ── Industrials / Defense ──────────────────────────────────────────────
    "CAT", "BA", "GE", "HON", "LMT", "RTX", "NOC", "DE", "ETN", "URI",
    # ── Materials / Commodities ────────────────────────────────────────────
    "FCX", "NEM", "AA", "CLF", "CE",
    # ── Telecom / Media / Entertainment ───────────────────────────────────
    "NFLX", "DIS", "CMCSA", "SPOT", "T", "VZ",
    # ── REITs ──────────────────────────────────────────────────────────────
    "AMT", "PLD", "EQIX", "SPG", "WELL",
    # ── Established fintech / growth ──────────────────────────────────────
    # (removed RIVN, LCID, HOOD — too speculative/low-price for stock buying)
    "COIN", "SOFI", "RBLX",
    # ── Sector ETFs (scanned for signals, no thesis card) ──────────────────
    "XLK", "XLE", "XLF", "XLV", "XLI", "XLC", "XLY", "XLP",
]

# These tickers are scanned for signals but NEVER get a full AI thesis card.
EXCLUDE_FROM_THESIS = {
    "SPY", "QQQ", "IWM", "DIA", "VTI", "ARKK",
    "XLK", "XLE", "XLF", "XLV", "XLI", "XLC", "XLY", "XLP", "XLRE", "XLU", "XLB",
    "GLD", "SLV", "USO", "UUP", "HYG", "TLT", "BND",
    "^VIX", "^TNX", "^IRX", "CL=F", "HG=F",
}

# ── Stock-quality filters ──────────────────────────────────────────────────
# These run on every candidate before the thesis is generated.
# They prevent penny stocks and low-liquidity names from auto-adding.
STOCK_MIN_PRICE     = 15.0   # skip anything trading below $15
STOCK_MIN_ADX       = 20     # skip weak-trend setups (ADX < 20 = no trend)
STOCK_MIN_MTF_SCORE = 2      # out of 3 (2 = weekly + daily bull, 3 = all three)

# Set to True to require a full entry signal; False also allows MACD crosses
REQUIRE_ENTRY_SIGNAL = True

# Max new stocks added to watchlist per day (prevents channel flooding)
MAX_ADDS_PER_DAY = 3

# ── Legacy aliases (keep for any old references) ───────────────────────────
MIN_MTF_SCORE    = STOCK_MIN_MTF_SCORE
REQUIRE_ENTRY    = REQUIRE_ENTRY_SIGNAL
