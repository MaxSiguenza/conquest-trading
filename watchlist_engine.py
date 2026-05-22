# -*- coding: utf-8 -*-
"""
Watchlist Engine
================
Fetches fundamental + price data via yfinance and stores watchlist entries
with AI-generated thesis, conviction rating, and price targets.

Storage: watchlist.json  (excluded from git via .gitignore)
"""

import os, sys, json, time
from datetime import datetime, timezone, date as _date

APP_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(APP_DIR, "watchlist.json")
sys.path.insert(0, APP_DIR)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_earnings_date(cal) -> str | None:
    """Safely extract next earnings date from yfinance calendar (dict or DataFrame)."""
    if cal is None:
        return None
    try:
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if ed is not None:
                val = list(ed)[0] if hasattr(ed, "__iter__") and not isinstance(ed, str) else ed
                s = str(val)
                return s[:10] if s and s not in ("NaT", "nan", "None") else None
        elif hasattr(cal, "index"):          # DataFrame
            if "Earnings Date" in cal.index:
                val = cal.loc["Earnings Date"]
                if hasattr(val, "iloc"):
                    val = val.iloc[0]
                s = str(val)
                return s[:10] if s and s not in ("NaT", "nan", "None") else None
    except Exception:
        pass
    return None


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_ticker_data(ticker: str) -> dict:
    """
    Pull all available yfinance fundamentals, valuation, analyst data,
    recent news headlines, and upcoming earnings date.
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

    # Recent news headlines (last 72h, up to 5 items)
    news_items = []
    try:
        now_ts = time.time()
        for item in (t.news or [])[:6]:
            title     = (item.get("title") or "")[:120]
            publisher = item.get("publisher", "")
            published = item.get("providerPublishTime", 0)
            age_h     = int((now_ts - published) / 3600) if published else 999
            if title and age_h <= 168:   # within 1 week
                news_items.append({
                    "title":     title,
                    "publisher": publisher,
                    "age_hours": age_h,
                })
    except Exception:
        pass

    # Upcoming earnings date
    next_earnings = None
    try:
        next_earnings = _parse_earnings_date(t.calendar)
    except Exception:
        pass

    # Live price — fast_info is more reliable than info["currentPrice"]
    price = 0.0
    try:
        fi    = t.fast_info
        price = float(getattr(fi, "last_price", 0) or 0)
    except Exception:
        pass
    if not price:
        price = float(
            info.get("currentPrice")
            or info.get("regularMarketPrice")
            or info.get("previousClose")
            or 0
        )

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
        # Live context
        "news":             news_items,
        "next_earnings":    next_earnings,
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

    # ── Recent news block ─────────────────────────────────────────────────────
    news_items = d.get("news", [])
    if news_items:
        news_lines = []
        for n in news_items[:5]:
            age = n.get("age_hours", 0)
            age_str = f"{age}h ago" if age < 48 else f"{age // 24}d ago"
            news_lines.append(
                f'  "{n["title"]}" — {n.get("publisher", "")} ({age_str})'
            )
        news_block = "\nRECENT NEWS (incorporate into thesis if relevant):\n" + "\n".join(news_lines)
    else:
        news_block = ""

    # ── Earnings date block ───────────────────────────────────────────────────
    next_earnings = d.get("next_earnings")
    if next_earnings and next_earnings not in ("nan", "NaT", "None"):
        try:
            from datetime import datetime as _dt2
            ed       = _dt2.strptime(next_earnings[:10], "%Y-%m-%d").date()
            days_to  = (ed - _date.today()).days
            if days_to >= 0:
                earnings_block = f"\nUPCOMING EARNINGS: {next_earnings[:10]} (in {days_to} days) — factor this into conviction and entry zone."
            else:
                earnings_block = f"\nMOST RECENT EARNINGS: {next_earnings[:10]} ({abs(days_to)} days ago)"
        except Exception:
            earnings_block = f"\nEARNINGS DATE: {next_earnings}"
    else:
        earnings_block = ""

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
{d.get('summary', 'N/A')}{news_block}{earnings_block}"""


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


def get_upcoming_earnings(days_ahead: int = 14) -> list:
    """
    Check all watchlist tickers for upcoming earnings within days_ahead days.
    Returns list of dicts sorted by days_to ascending:
      {ticker, name, earnings_date, days_to, conviction, thesis}
    """
    import yfinance as yf

    upcoming = []
    for e in load_watchlist():
        ticker = e.get("ticker", "")
        if not ticker:
            continue
        try:
            cal           = yf.Ticker(ticker).calendar
            next_earnings = _parse_earnings_date(cal)
            if not next_earnings or next_earnings in ("nan", "NaT", "None"):
                continue
            from datetime import datetime as _dt2
            ed      = _dt2.strptime(next_earnings[:10], "%Y-%m-%d").date()
            days_to = (ed - _date.today()).days
            if 0 <= days_to <= days_ahead:
                upcoming.append({
                    "ticker":        ticker,
                    "name":          e.get("name", ticker),
                    "earnings_date": next_earnings[:10],
                    "days_to":       days_to,
                    "conviction":    e.get("conviction", "MEDIUM"),
                    "thesis":        (e.get("thesis") or "")[:200],
                    "entry_zone":    e.get("entry_zone", ""),
                    "hard_stop":     e.get("hard_stop", ""),
                })
        except Exception:
            pass

    upcoming.sort(key=lambda x: x["days_to"])
    return upcoming


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
