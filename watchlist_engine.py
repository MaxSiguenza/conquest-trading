# -*- coding: utf-8 -*-
"""
Watchlist Engine
================
Fetches fundamental + price data via yfinance and stores watchlist entries
with AI-generated thesis, conviction rating, and price targets.

Storage: watchlist.json  (excluded from git via .gitignore)
"""

import os, sys, json
from datetime import datetime, timezone, date as _date

APP_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(APP_DIR, "watchlist.json")
sys.path.insert(0, APP_DIR)


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_ticker_data(ticker: str) -> dict:
    """
    Pull all available yfinance fundamentals, valuation, and analyst data.
    Returns a flat dict — missing fields are None.
    """
    import yfinance as yf

    ticker = ticker.upper().strip()
    t      = yf.Ticker(ticker)

    info = {}
    try:
        info = t.info or {}
    except Exception:
        pass

    # 30-day price change
    price_30d = 0.0
    try:
        hist = t.history(period="35d")
        if len(hist) >= 22:
            price_30d = float(hist["Close"].iloc[-1] / hist["Close"].iloc[-22] - 1) * 100
        elif len(hist) >= 2:
            price_30d = float(hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100
    except Exception:
        pass

    # YTD price change
    price_ytd = 0.0
    try:
        ytd_start = f"{_date.today().year}-01-01"
        ytd = t.history(start=ytd_start)
        if len(ytd) >= 2:
            price_ytd = float(ytd["Close"].iloc[-1] / ytd["Close"].iloc[0] - 1) * 100
    except Exception:
        pass

    price = (info.get("currentPrice")
             or info.get("regularMarketPrice")
             or info.get("previousClose")
             or 0)

    return {
        "ticker":           ticker,
        "name":             info.get("longName") or info.get("shortName") or ticker,
        "sector":           info.get("sector", "Unknown"),
        "industry":         info.get("industry", "Unknown"),
        "price":            round(float(price), 2),
        "market_cap":       info.get("marketCap"),
        "price_30d_chg":    round(price_30d, 2),
        "price_ytd_chg":    round(price_ytd, 2),
        # Valuation
        "forward_pe":       info.get("forwardPE"),
        "trailing_pe":      info.get("trailingPE"),
        "ev_ebitda":        info.get("enterpriseToEbitda"),
        "price_to_sales":   info.get("priceToSalesTrailingTwelveMonths"),
        "price_to_book":    info.get("priceToBook"),
        # Growth
        "revenue_growth":   info.get("revenueGrowth"),
        "earnings_growth":  info.get("earningsGrowth"),
        "gross_margin":     info.get("grossMargins"),
        "operating_margin": info.get("operatingMargins"),
        # Balance sheet
        "total_cash":       info.get("totalCash"),
        "total_debt":       info.get("totalDebt"),
        "shares_out":       info.get("sharesOutstanding"),
        # EPS
        "eps_forward":      info.get("forwardEps"),
        "eps_trailing":     info.get("trailingEps"),
        # Analyst consensus
        "target_mean":      info.get("targetMeanPrice"),
        "target_high":      info.get("targetHighPrice"),
        "target_low":       info.get("targetLowPrice"),
        "analyst_count":    info.get("numberOfAnalystOpinions"),
        "recommendation":   info.get("recommendationKey", ""),
        # Risk metrics
        "beta":             info.get("beta"),
        "52w_high":         info.get("fiftyTwoWeekHigh"),
        "52w_low":          info.get("fiftyTwoWeekLow"),
        "short_ratio":      info.get("shortRatio"),
        # Business summary
        "summary":          (info.get("longBusinessSummary") or "")[:600],
    }


def _fmt(n, prefix="$", billions=True) -> str:
    """Format a large number for display."""
    if n is None:
        return "N/A"
    n = float(n)
    if billions and abs(n) >= 1e9:
        return f"{prefix}{n/1e9:.1f}B"
    if abs(n) >= 1e6:
        return f"{prefix}{n/1e6:.0f}M"
    return f"{prefix}{n:.2f}"


def build_data_block(data: dict) -> str:
    """
    Format fetched data as a structured text block
    suitable for pasting into Claude prompts.
    """
    d = data

    def pct(v):
        return f"{v*100:+.1f}%" if v is not None else "N/A"

    def val(v, fmt=".2f"):
        return f"{v:{fmt}}" if v is not None else "N/A"

    upside = ""
    if d.get("target_mean") and d.get("price") and d["price"] > 0:
        up = (d["target_mean"] / d["price"] - 1) * 100
        upside = f"  ({up:+.0f}% to consensus target)"

    return f"""TICKER: {d['ticker']} — {d.get('name', '')}
SECTOR: {d.get('sector', '')} / {d.get('industry', '')}

PRICE & PERFORMANCE
  Current:     ${d.get('price', 0):.2f}
  Market cap:  {_fmt(d.get('market_cap'))}
  30-day chg:  {d.get('price_30d_chg', 0):+.1f}%
  YTD chg:     {d.get('price_ytd_chg', 0):+.1f}%
  52w range:   ${val(d.get('52w_low'), ',.2f')} – ${val(d.get('52w_high'), ',.2f')}
  Beta:        {val(d.get('beta'))}
  Short ratio: {val(d.get('short_ratio'))} days to cover

VALUATION
  Forward P/E: {val(d.get('forward_pe'))}
  Trailing P/E:{val(d.get('trailing_pe'))}
  EV/EBITDA:   {val(d.get('ev_ebitda'))}
  P/Sales:     {val(d.get('price_to_sales'))}
  P/Book:      {val(d.get('price_to_book'))}

GROWTH & PROFITABILITY
  Revenue growth:   {pct(d.get('revenue_growth'))} YoY
  Earnings growth:  {pct(d.get('earnings_growth'))} YoY
  Gross margin:     {pct(d.get('gross_margin'))}
  Operating margin: {pct(d.get('operating_margin'))}
  Forward EPS:  ${val(d.get('eps_forward'))}
  Trailing EPS: ${val(d.get('eps_trailing'))}

BALANCE SHEET
  Cash:  {_fmt(d.get('total_cash'))}
  Debt:  {_fmt(d.get('total_debt'))}
  Net:   {_fmt((d.get('total_cash') or 0) - (d.get('total_debt') or 0))}

ANALYST CONSENSUS ({d.get('analyst_count', 0)} analysts)
  Mean target: ${val(d.get('target_mean'))}{upside}
  Range:       ${val(d.get('target_low'))} – ${val(d.get('target_high'))}
  Rating:      {d.get('recommendation', '').upper()}

BUSINESS SUMMARY:
{d.get('summary', 'N/A')}"""


# ── Storage ───────────────────────────────────────────────────────────────────

def load_watchlist() -> list:
    """Return all watchlist entries (newest first)."""
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_entry(entry: dict):
    """Insert or replace a watchlist entry by ticker."""
    entries = load_watchlist()
    entries = [e for e in entries if e.get("ticker") != entry.get("ticker")]
    entries.insert(0, entry)
    with open(DATA_FILE, "w") as f:
        json.dump(entries, f, indent=2)


def remove_entry(ticker: str) -> bool:
    """Remove a ticker. Returns True if it was found and removed."""
    entries  = load_watchlist()
    filtered = [e for e in entries if e.get("ticker", "").upper() != ticker.upper()]
    if len(filtered) < len(entries):
        with open(DATA_FILE, "w") as f:
            json.dump(filtered, f, indent=2)
        return True
    return False


def get_entry(ticker: str) -> dict:
    """Return one entry by ticker, or {}."""
    for e in load_watchlist():
        if e.get("ticker", "").upper() == ticker.upper():
            return e
    return {}


# ── Analysis pipeline ─────────────────────────────────────────────────────────

def analyze_and_add(ticker: str) -> dict:
    """
    Full watchlist entry pipeline:
      1. Fetch yfinance data
      2. Run thesis + scenarios (Prompts 1+2+3)
      3. Run risk assessment (Prompt 6)
      4. Combine, save, and return the entry

    Takes ~15-25 seconds (two Claude API calls).
    """
    from conquest_brain import watchlist_thesis, watchlist_risks

    ticker    = ticker.upper().strip()
    data      = fetch_ticker_data(ticker)
    block     = build_data_block(data)
    thesis_d  = watchlist_thesis(ticker, block)
    risk_d    = watchlist_risks(ticker, block)

    entry = {
        "ticker":       ticker,
        "name":         data.get("name", ticker),
        "sector":       data.get("sector", ""),
        "industry":     data.get("industry", ""),
        "price":        data.get("price", 0),
        "market_cap":   data.get("market_cap"),
        "added_at":     datetime.now(timezone.utc).isoformat(),
        # Thesis output
        "thesis":           thesis_d.get("thesis", ""),
        "narrative_hook":   thesis_d.get("narrative_hook", ""),
        "fundamentals":     thesis_d.get("fundamentals", ""),
        "conviction":       thesis_d.get("conviction", "MEDIUM"),
        "waiting_for":      thesis_d.get("waiting_for", ""),
        "entry_zone":       thesis_d.get("entry_zone", ""),
        "hard_stop":        thesis_d.get("hard_stop", ""),
        "bear_target":      thesis_d.get("bear_target", ""),
        "base_target":      thesis_d.get("base_target", ""),
        "bull_target":      thesis_d.get("bull_target", ""),
        # Risk output
        "risks":            risk_d.get("risks", ""),
        "risk_flags":       risk_d.get("risk_flags", []),
        # Raw fundamentals for display
        "forward_pe":       data.get("forward_pe"),
        "revenue_growth":   data.get("revenue_growth"),
        "target_mean":      data.get("target_mean"),
        "recommendation":   data.get("recommendation", ""),
        "analyst_count":    data.get("analyst_count"),
    }

    save_entry(entry)
    return entry


def deep_dive(ticker: str) -> tuple:
    """
    Full 6-prompt deep dive.
    Returns (entry_dict, deep_dive_text) — entry is saved to watchlist.json.
    Takes ~30-40 seconds.
    """
    from conquest_brain import watchlist_thesis, watchlist_risks, watchlist_deep_dive_report

    ticker = ticker.upper().strip()
    data   = fetch_ticker_data(ticker)
    block  = build_data_block(data)

    thesis_d   = watchlist_thesis(ticker, block)
    risk_d     = watchlist_risks(ticker, block)
    deep_text  = watchlist_deep_dive_report(ticker, block)

    entry = {
        "ticker":       ticker,
        "name":         data.get("name", ticker),
        "sector":       data.get("sector", ""),
        "price":        data.get("price", 0),
        "market_cap":   data.get("market_cap"),
        "added_at":     datetime.now(timezone.utc).isoformat(),
        "thesis":           thesis_d.get("thesis", ""),
        "narrative_hook":   thesis_d.get("narrative_hook", ""),
        "fundamentals":     thesis_d.get("fundamentals", ""),
        "conviction":       thesis_d.get("conviction", "MEDIUM"),
        "waiting_for":      thesis_d.get("waiting_for", ""),
        "entry_zone":       thesis_d.get("entry_zone", ""),
        "hard_stop":        thesis_d.get("hard_stop", ""),
        "bear_target":      thesis_d.get("bear_target", ""),
        "base_target":      thesis_d.get("base_target", ""),
        "bull_target":      thesis_d.get("bull_target", ""),
        "risks":            risk_d.get("risks", ""),
        "risk_flags":       risk_d.get("risk_flags", []),
        "deep_dive":        deep_text,
        "forward_pe":       data.get("forward_pe"),
        "revenue_growth":   data.get("revenue_growth"),
        "target_mean":      data.get("target_mean"),
        "recommendation":   data.get("recommendation", ""),
    }

    save_entry(entry)
    return entry, deep_text
