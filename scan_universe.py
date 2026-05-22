# -*- coding: utf-8 -*-
"""
Conquest Scan Universe
======================
The master list of stocks the system automatically scans each morning.
When a ticker fires a real entry signal (MTF ≥ 2, RSI + ADX confirmed),
the bot runs the full AI thesis and posts it to #watchlist automatically.

Edit the UNIVERSE list freely — add tickers, remove ones you don't care about.
Keep it under ~120 tickers for reasonable scan speed.

ETFs and indexes in EXCLUDE_FROM_THESIS are scanned for signals but never
get a full AI thesis card (no point writing a "conviction" report on SPY).
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
    # ── High-growth / Fintech / Speculative ────────────────────────────────
    "COIN", "SOFI", "AFRM", "RBLX", "U", "RIVN", "LCID",
    # ── Sector ETFs (scanned for signals, no thesis card) ──────────────────
    "XLK", "XLE", "XLF", "XLV", "XLI", "XLC", "XLY", "XLP",
]

# These tickers are scanned for signals but NEVER get a full AI thesis card.
# Broad ETFs and indexes don't need conviction write-ups.
EXCLUDE_FROM_THESIS = {
    "SPY", "QQQ", "IWM", "DIA", "VTI", "ARKK",
    "XLK", "XLE", "XLF", "XLV", "XLI", "XLC", "XLY", "XLP", "XLRE", "XLU", "XLB",
    "GLD", "SLV", "USO", "UUP", "HYG", "TLT", "BND",
    "^VIX", "^TNX", "^IRX", "CL=F", "HG=F",
}

# Minimum signal quality to trigger an auto-thesis + watchlist add.
# Tune these thresholds to control how often stocks get auto-added.
MIN_MTF_SCORE  = 2      # out of 3 (2 = weekly + daily bull, 3 = all three)
REQUIRE_ENTRY  = True   # True = only full entry signals; False = also MACD crosses
MAX_ADDS_PER_DAY = 3    # cap so the channel doesn't get flooded on busy days
