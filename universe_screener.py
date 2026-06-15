# -*- coding: utf-8 -*-
"""
Universe Pre-Screener
=====================
Lightweight momentum pre-screen over ~120 S&P 500 names.
No AI calls — pure technical signals (yfinance + existing scanner).
Returns the top N candidates by signal strength for the agent swarm.

Runs once per day before the 6-agent swarm launches.
Replaces the hardcoded 20-ticker PAPER_UNIVERSE with a data-driven
selection across all major sectors, expanding opportunity set 6×.

Scoring (per ticker):
  +2.0 × MTF score (0–3)     → max +6.0
  +3.0   entry_signal fired   → strong binary trigger
  +1.5   squeeze fired        → momentum pop
  +0.5   squeeze coiling      → setup forming
  +1.0   MACD cross up        → trend confirmation
  +0–1   ADX strength bonus   → trend quality
  +/-0.5 HV rank relative     → slight IV edge
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── ~120 high-liquidity S&P 500 names across all 11 GICS sectors ─────────────
# Deliberately broad — the screener's job is to narrow this down, not the list's
SP500_UNIVERSE = [

    # ── Technology ──────────────────────────────────────────────────────────
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AVGO", "ORCL", "CRM",
    "AMD",  "QCOM", "TXN",  "MU",    "AMAT", "NOW",  "ADSK", "PANW",
    "CRWD", "SNOW", "PLTR", "NET",   "TTD",  "DDOG", "FTNT", "INTC",

    # ── Healthcare ──────────────────────────────────────────────────────────
    "JNJ",  "LLY",  "ABBV", "UNH",  "MRK",  "PFE",  "TMO",  "ABT",
    "DHR",  "AMGN", "GILD", "VRTX", "REGN", "ISRG", "CI",   "CVS",
    "HUM",  "BMY",

    # ── Financials ──────────────────────────────────────────────────────────
    "JPM",  "BAC",  "WFC",  "GS",   "MS",   "BLK",  "V",    "MA",
    "AXP",  "SPGI", "ICE",  "CME",  "C",    "COF",  "SCHW",

    # ── Energy ──────────────────────────────────────────────────────────────
    "XOM",  "CVX",  "COP",  "EOG",  "SLB",  "PSX",  "VLO",  "OXY",
    "DVN",  "MPC",

    # ── Consumer Discretionary ──────────────────────────────────────────────
    "AMZN", "TSLA", "HD",   "MCD",  "NKE",  "LOW",  "SBUX", "TGT",
    "BKNG", "MAR",  "HLT",  "ABNB", "RCL",  "F",    "GM",

    # ── Consumer Staples ────────────────────────────────────────────────────
    "WMT",  "PG",   "KO",   "PEP",  "COST", "MDLZ", "CL",   "MO",
    "PM",

    # ── Industrials ─────────────────────────────────────────────────────────
    "CAT",  "DE",   "HON",  "UNP",  "UPS",  "FDX",  "GE",   "LMT",
    "RTX",  "NOC",  "BA",   "ETN",  "ITW",  "EMR",

    # ── Communication Services ──────────────────────────────────────────────
    "NFLX", "DIS",  "CMCSA","VZ",   "T",    "TMUS",

    # ── Materials ───────────────────────────────────────────────────────────
    "LIN",  "APD",  "NEM",  "FCX",  "SHW",  "DD",

    # ── Broad ETFs (liquid, ideal for condors / spreads) ────────────────────
    "SPY",  "QQQ",  "IWM",  "XLF",  "XLE",  "XLK",  "XLV",
    "GLD",  "TLT",  "XLI",  "XLY",  "XLP",
]


def _score_scan(r: dict) -> float:
    """
    Convert a scan result dict into a single float ranking score.
    Higher = stronger setup. Negative = broken data.
    """
    if r.get("error") or not r.get("price"):
        return -999.0

    s = 0.0
    s += r.get("mtf_score", 0) * 2.0           # 0–6 (most important)
    s += 3.0 if r.get("entry_signal")  else 0  # full entry signal
    s += 1.5 if r.get("sqz_fired")    else 0  # squeeze momentum pop
    s += 0.5 if r.get("sqz_on")       else 0  # squeeze coiling (pre-pop)
    s += 1.0 if r.get("macd_cross_up") else 0  # trend confirmation

    # ADX: trend strength 0→50+ capped at 1.0 bonus
    adx = float(r.get("adx") or 0)
    s += min(adx / 50.0, 1.0)

    # HV rank relative to 50 (slight edge to names with more IV premium)
    hv_rank = float(r.get("hv_rank") or 50)
    s += 0.5 * ((hv_rank - 50.0) / 50.0)

    return round(s, 4)


# Module-level cache so Discord bot can read results after trade generation runs
_last_screen_results: list = []   # list of scored scan dicts from last run


def get_last_screen() -> list:
    """Return the full scored results from the most recent pre_screen() call."""
    return list(_last_screen_results)


def pre_screen(n: int = 40, universe: list = None, workers: int = 6) -> list:
    """
    Run a lightweight momentum pre-screen over the full universe.
    Returns the top `n` ticker symbols ranked by signal strength.

    No AI calls, no extra API costs — just the same scan_ticker()
    used by the existing alerts system.  Typically 20–35 sec.

    Args:
        n:        How many tickers to return for the agent swarm.
        universe: Custom list to scan (defaults to SP500_UNIVERSE).
        workers:  Thread-pool size for parallel yfinance pulls.

    Returns:
        List of ticker strings, highest-signal first.
    """
    universe = universe or SP500_UNIVERSE
    print(f"[PreScreener] Scanning {len(universe)} tickers -> picking top {n}...")

    try:
        from alerts.scanner import scan_ticker
    except ImportError:
        print("[PreScreener] scanner unavailable — falling back to universe[:n]")
        return universe[:n]

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(scan_ticker, t): t for t in universe}
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r and not r.get("error") and (r.get("price") or 0) > 0:
                    r["_score"] = _score_scan(r)
                    results.append(r)
            except Exception:
                pass

    results.sort(key=lambda r: r["_score"], reverse=True)
    global _last_screen_results
    _last_screen_results = results   # cache for Discord bot to read
    import gc; gc.collect()  # release DataFrames freed inside scan_ticker

    top_tickers = [r["ticker"] for r in results[:n]]

    # Sector diversity guard: make sure SPY + QQQ are included as anchors
    # (good iron condor candidates and broad-market calibration)
    for anchor in ("SPY", "QQQ"):
        if anchor not in top_tickers and anchor in universe:
            if len(top_tickers) >= n:
                top_tickers[-1] = anchor   # swap out the weakest scorer
            else:
                top_tickers.append(anchor)

    if results:
        best = results[0]
        print(
            f"[PreScreener] Top pick: {best['ticker']} "
            f"(score={best['_score']:.2f}, MTF={best.get('mtf_score',0)}, "
            f"entry={'✓' if best.get('entry_signal') else '–'}, "
            f"sqz={'🔥' if best.get('sqz_fired') else '–'})"
        )
    print(f"[PreScreener] Candidates: {' '.join(top_tickers)}")
    return top_tickers


def get_sector_map() -> dict:
    """
    Returns a rough sector label for each ticker in SP500_UNIVERSE.
    Used by the correlation filter to flag sector concentration.
    """
    return {
        # Tech
        **{t: "Technology" for t in [
            "AAPL","MSFT","NVDA","GOOGL","META","AVGO","ORCL","CRM","AMD",
            "QCOM","TXN","MU","AMAT","NOW","ADSK","PANW","CRWD","SNOW",
            "PLTR","NET","TTD","DDOG","FTNT","INTC"
        ]},
        # Healthcare
        **{t: "Healthcare" for t in [
            "JNJ","LLY","ABBV","UNH","MRK","PFE","TMO","ABT","DHR",
            "AMGN","GILD","VRTX","REGN","ISRG","CI","CVS","HUM","BMY"
        ]},
        # Financials
        **{t: "Financials" for t in [
            "JPM","BAC","WFC","GS","MS","BLK","V","MA","AXP",
            "SPGI","ICE","CME","C","COF","SCHW"
        ]},
        # Energy
        **{t: "Energy" for t in [
            "XOM","CVX","COP","EOG","SLB","PSX","VLO","OXY","DVN","MPC"
        ]},
        # Consumer Disc
        **{t: "Cons.Discret" for t in [
            "AMZN","TSLA","HD","MCD","NKE","LOW","SBUX","TGT",
            "BKNG","MAR","HLT","ABNB","RCL","F","GM"
        ]},
        # Consumer Staples
        **{t: "Cons.Staples" for t in [
            "WMT","PG","KO","PEP","COST","MDLZ","CL","MO","PM"
        ]},
        # Industrials
        **{t: "Industrials" for t in [
            "CAT","DE","HON","UNP","UPS","FDX","GE","LMT","RTX","NOC","BA","ETN","ITW","EMR"
        ]},
        # Comm
        **{t: "Comm.Services" for t in ["NFLX","DIS","CMCSA","VZ","T","TMUS"]},
        # Materials
        **{t: "Materials" for t in ["LIN","APD","NEM","FCX","SHW","DD"]},
        # ETFs
        **{t: "ETF" for t in ["SPY","QQQ","IWM","XLF","XLE","XLK","XLV","GLD","TLT","XLI","XLY","XLP"]},
    }
