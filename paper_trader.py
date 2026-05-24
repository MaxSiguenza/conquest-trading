# -*- coding: utf-8 -*-
"""
Conquest Trading — Automated Paper Trade Engine
=================================================
Generates 10 paper trades per day across all instrument types.
Marks to market daily and closes on profit/stop targets.
Run via APScheduler (web_app.py) or manually:
  python paper_trader.py          — generate today's trades
  python paper_trader.py close    — mark open trades + apply stops/targets
  python paper_trader.py stats    — print performance summary
"""

import json
import os
import random
import sys
from datetime import datetime, date, timedelta
from typing import Optional

import pytz

APP_DIR   = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG = os.path.join(APP_DIR, "paper_trades.json")
ET        = pytz.timezone("America/New_York")

# ── Extended universe (20 liquid names for variety) ───────────────────────────
PAPER_UNIVERSE = [
    "AAPL", "NVDA", "MSFT", "GOOGL", "AMZN",
    "META", "TSLA", "JPM",  "XOM",   "WMT",
    "COP",  "SPY",  "QQQ",  "AMD",   "NFLX",
    "COST", "V",    "MA",   "BAC",   "DIS",
]

# ── Risk / sizing ──────────────────────────────────────────────────────────────
STOCK_SIZE       = 1_000   # $ notional per stock trade
OPTION_CONTRACTS = 1       # 1 contract = 100 shares
RISK_FREE        = 0.05    # 5 % annualised
DTE_TARGET       = 30      # days to expiry at entry
MAX_HOLD_STK     = 5       # stocks: close after 5 calendar days regardless
MAX_HOLD_OPT     = 21      # options: close after 21 days (bought at 30 DTE, near expiry)

# ── Stock exit thresholds (fixed — signal confirmed these work) ────────────────
STK_PROFIT   =  0.05   # close stock  when up   5 %
STK_STOP     = -0.03   # close stock  when down 3 %

# ── Options exits: AI agent-driven, not fixed percentages ─────────────────────
# The agent exit reviewer runs at daily close and makes hold/close decisions
# based on current conditions, days remaining, and whether the thesis still holds.
# BACKSTOP_OPT is a safety net only — prevents total wipeout, not a target.
BACKSTOP_OPT = -0.80   # emergency backstop: close if option loses > 80 % of value

# ── Iron condor exits (credit trade — these remain rules-based) ───────────────
IC_PROFIT    =  0.50   # close iron condor when 50 % of max credit earned
IC_STOP      =  2.00   # close iron condor when position costs 2× credit (loss)


def _third_friday(year: int, month: int) -> date:
    """Third Friday of a given month — standard US options expiration date."""
    d = date(year, month, 1)
    days_to_first_fri = (4 - d.weekday()) % 7
    return d + timedelta(days=days_to_first_fri + 14)   # first Friday + 2 weeks


def _options_expiry(entry: date, dte: int = DTE_TARGET) -> str:
    """
    Standard monthly options expiration closest to entry + dte days.
    Uses the third Friday of the month — the date every broker (Robinhood,
    Tastytrade, etc.) lists for standard options on every ticker.
    Requires at least 21 days from entry so we don't pick an expiry that's
    already too close to worthless at the time we enter.
    """
    target = entry + timedelta(days=dte)

    # Build candidate third-Fridays spanning ±2 months around target
    candidates: list[date] = []
    for delta_m in range(-1, 4):
        m = target.month + delta_m
        y = target.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        candidates.append(_third_friday(y, m))

    # Must be at least 21 DTE from entry (no point buying near-expiry options)
    valid = [c for c in candidates if (c - entry).days >= 21]

    # Return the one closest in calendar days to our target date
    return min(valid, key=lambda c: abs((c - target).days)).isoformat()


# ── Persistence ───────────────────────────────────────────────────────────────

def load_trades() -> list:
    from db import kv_get
    data = kv_get("paper_trades")
    return data if isinstance(data, list) else []


def save_trades(trades: list) -> None:
    from db import kv_set
    kv_set("paper_trades", trades)


# ── Pricing helpers ────────────────────────────────────────────────────────────

def _bs(S: float, K: float, T: float, sigma: float, opt_type: str = "call") -> float:
    """Black-Scholes wrapper — returns 0 gracefully on bad inputs."""
    try:
        from models.black_scholes import black_scholes_price
        if T <= 0:
            return max(S - K, 0.0) if opt_type == "call" else max(K - S, 0.0)
        return float(black_scholes_price(S, K, T, RISK_FREE, sigma, opt_type))
    except Exception:
        return 0.0


def _greeks(S: float, K: float, T: float, sigma: float,
            opt_type: str = "call") -> dict:
    """
    Black-Scholes Greeks — per share, per day for theta.

    Returns delta, theta ($/share/day), gamma, vega (per 1% IV move).
    All values are from the LONG perspective:
      • Long call:  delta > 0,  theta < 0,  gamma > 0,  vega > 0
      • Long put:   delta < 0,  theta < 0,  gamma > 0,  vega > 0
    For short positions, flip the signs at the call site.
    """
    try:
        from scipy.stats import norm
        import math
        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
            return {"delta": 0.0, "theta": 0.0, "gamma": 0.0, "vega": 0.0}
        d1  = (math.log(S / K) + (RISK_FREE + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2  = d1 - sigma * math.sqrt(T)
        pdf = float(norm.pdf(d1))
        gamma = pdf / (S * sigma * math.sqrt(T))
        vega  = S * pdf * math.sqrt(T) / 100          # per 1 % IV change
        if opt_type == "call":
            delta = float(norm.cdf(d1))
            theta = (-(S * pdf * sigma) / (2 * math.sqrt(T))
                     - RISK_FREE * K * math.exp(-RISK_FREE * T) * float(norm.cdf(d2))) / 365
        else:
            delta = float(norm.cdf(d1)) - 1.0
            theta = (-(S * pdf * sigma) / (2 * math.sqrt(T))
                     + RISK_FREE * K * math.exp(-RISK_FREE * T) * float(norm.cdf(-d2))) / 365
        return {
            "delta": round(delta,  4),
            "theta": round(theta,  4),   # per share per day (negative = long option pays theta)
            "gamma": round(gamma,  4),
            "vega":  round(vega,   4),
        }
    except Exception:
        return {"delta": 0.0, "theta": 0.0, "gamma": 0.0, "vega": 0.0}


def _hv(ticker: str, fallback: float = 0.28) -> float:
    """30-day historical annualised volatility."""
    try:
        import yfinance as yf
        import numpy as np
        df = yf.download(ticker, period="60d", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 10:
            return fallback
        rets = float(df["Close"].pct_change().dropna().std()) * (252 ** 0.5)
        return max(rets, 0.05)   # floor at 5 %
    except Exception:
        return fallback


def _live_price(ticker: str) -> Optional[float]:
    try:
        import yfinance as yf
        p = float(yf.Ticker(ticker).fast_info["lastPrice"])
        return p if p > 0 else None
    except Exception:
        return None


def _round_strike(price: float, step: float) -> float:
    return round(round(price / step) * step, 2)


def _strike_step(price: float) -> float:
    if price >= 200:   return 5.0
    if price >= 50:    return 2.5
    if price >= 20:    return 1.0
    return 0.5


# ── Trade-type assignment ──────────────────────────────────────────────────────

_TYPE_WEIGHTS = {
    "stock_long":       1,
    # stock_short removed — backtest confirmed 42.8% WR, -$6,231 drag over 2y
    "call_spread":      1,   # bull call debit spread (directional)
    "put_spread":       1,   # bear put debit spread (directional)
    "long_call":        1,
    "long_put":         1,
    "iron_condor":      2,
    # ── Premium collection (new) — collect IV premium instead of paying it ──
    "bull_put_spread":  2,   # credit put spread: bullish, collects premium
    "bear_call_spread": 2,   # credit call spread: bearish/neutral, collects premium
    "covered_call":     2,   # sell OTM call for income on bullish stocks
}

def _assign_trade_type(scan: dict) -> str:
    """
    Signal-driven trade-type selection.
    Directional signals → directional trades.
    High IV / neutral → iron condor or credit spread.
    """
    daily      = scan.get("daily",  "BULL")
    weekly     = scan.get("weekly", "BULL")
    rsi        = scan.get("rsi",    50.0)
    hv_rank    = scan.get("hv_rank", 50.0)   # scanner returns 0-100 scale
    sqz_fired  = scan.get("sqz_fired", False)
    sqz_mom    = scan.get("sqz_momentum", 0.0)
    adx        = scan.get("adx",   20.0)
    entry_sig  = scan.get("entry_signal", False)

    bullish  = (daily == "BULL") and (weekly == "BULL")
    bearish  = (daily == "BEAR") and (weekly == "BEAR")
    trending = adx > 22
    # Options are cheap when HV rank is below 35th percentile — good time to buy.
    # Above that, the IV premium erodes directional trades before they start.
    iv_cheap = hv_rank < 35

    # ── High IV: premium is expensive → SELL it, don't buy it ───────────────
    if hv_rank > 65 and not sqz_fired:
        if bullish and trending:
            # Bull put spread: collect fat premium, profit if stock holds above short put
            return random.choice(["bull_put_spread", "bull_put_spread", "covered_call"])
        if bearish and trending:
            # Bear call spread: collect fat premium, profit if stock stays below short call
            return random.choice(["bear_call_spread", "bear_call_spread", "iron_condor"])
        return "iron_condor"

    # ── Strong bullish + squeeze firing: directional momentum play ────────────
    if bullish and trending:
        if sqz_fired and sqz_mom > 0:
            # Squeeze breakout + cheap IV = ideal conditions for long calls.
            # Expensive IV → collect premium instead; the move is already priced in.
            if iv_cheap:
                return random.choice(["long_call", "bull_put_spread", "covered_call", "stock_long"])
            else:
                return random.choice(["bull_put_spread", "covered_call", "stock_long"])
        if entry_sig and rsi < 45:
            # Clean entry signal, not overbought. Only buy calls if IV is cheap.
            if iv_cheap:
                return random.choice(["long_call", "stock_long", "bull_put_spread"])
            else:
                return random.choice(["stock_long", "bull_put_spread"])
        # General bullish without squeeze or cheap IV → lean income
        return random.choice(["bull_put_spread", "covered_call", "stock_long",
                               "covered_call", "bull_put_spread"])

    # ── Bearish: lean credit over debit (IV premium problem on long puts) ─────
    if bearish and trending:
        if sqz_fired and sqz_mom < 0:
            # Only buy puts when IV is genuinely cheap
            if iv_cheap:
                return random.choice(["bear_call_spread", "long_put", "bear_call_spread"])
            else:
                return random.choice(["bear_call_spread", "bear_call_spread"])
        if rsi > 58:
            return random.choice(["bear_call_spread", "long_put"]) if iv_cheap else "bear_call_spread"
        return random.choice(["bear_call_spread", "bear_call_spread", "long_put"]) if iv_cheap \
            else random.choice(["bear_call_spread", "bear_call_spread"])

    # ── Neutral / mixed: range-bound → premium collection is king ────────────
    if hv_rank > 40:
        return random.choice(["iron_condor", "bull_put_spread", "bear_call_spread"])
    return random.choice(["iron_condor", "covered_call", "bull_put_spread"])


# ── Live options chain query ───────────────────────────────────────────────────

def _live_chain(ticker: str, opt_type: str, target_strike: float,
                entry: "date", dte_target: int = DTE_TARGET) -> dict | None:
    """
    Query yfinance for the real options chain and return the best matching
    contract for our trade.  Falls back to None on any failure so callers
    can gracefully use Black-Scholes instead.

    Returns:
        {expiry, strike, mid, bid, ask, iv, dte_actual, source="live"}
    """
    try:
        import yfinance as yf
        tk   = yf.Ticker(ticker)
        exps = tk.options          # tuple of "YYYY-MM-DD" strings
        if not exps:
            return None

        # Filter expirations: must be at least 14 DTE from entry
        valid = [e for e in exps if (date.fromisoformat(e) - entry).days >= 14]
        if not valid:
            return None

        # Pick the expiry closest to our target DTE
        best_exp  = min(valid, key=lambda e: abs((date.fromisoformat(e) - entry).days - dte_target))
        actual_dte = (date.fromisoformat(best_exp) - entry).days

        # Pull the chain for that expiry
        chain = tk.option_chain(best_exp)
        df    = chain.calls if opt_type == "call" else chain.puts
        if df is None or df.empty:
            return None

        # Prefer liquid contracts (has an ask price)
        liquid = df[df["ask"].fillna(0) > 0]
        pool   = liquid if not liquid.empty else df

        # Find the strike nearest our target
        idx = (pool["strike"] - target_strike).abs().idxmin()
        row = pool.loc[idx]

        bid = float(row.get("bid") or 0)
        ask = float(row.get("ask") or 0)
        mid = round((bid + ask) / 2, 4) if ask > 0 else 0.0
        iv  = float(row.get("impliedVolatility") or 0)

        if mid <= 0.01:   # no real market — too illiquid
            return None

        return {
            "expiry":     best_exp,
            "strike":     float(row["strike"]),
            "mid":        mid,
            "bid":        round(bid, 4),
            "ask":        round(ask, 4),
            "iv":         round(iv, 4),
            "dte_actual": actual_dte,
            "source":     "live",
        }
    except Exception:
        return None


# ── Trade builders ─────────────────────────────────────────────────────────────

def _build_stock(scan: dict, trade_type: str, ts: str) -> Optional[dict]:
    price = scan.get("price", 0)
    if not price:
        return None
    shares = round(STOCK_SIZE / price, 4)
    return {
        "id":           f"{ts}_{scan['ticker']}_{trade_type}",
        "date_entered": ts,
        "ticker":       scan["ticker"],
        "trade_type":   trade_type,
        "status":       "open",
        "side":         "long" if trade_type == "stock_long" else "short",
        "entry_price":  round(price, 4),
        "shares":       shares,
        "cost_basis":   round(price * shares, 2),
        "current_price": round(price, 4),
        "pnl":          0.0,
        "pnl_pct":      0.0,
        "date_closed":  None,
        "close_reason": None,
        "days_held":    0,
        # signal context (for review)
        "mtf_score":    scan.get("mtf_score", 0),
        "rsi_entry":    round(scan.get("rsi", 50), 1),
    }


def _build_option(scan: dict, trade_type: str, ts: str) -> Optional[dict]:
    price = scan.get("price", 0)
    if not price:
        return None

    opt_type    = "call" if trade_type == "long_call" else "put"
    step        = _strike_step(price)
    target_k    = _round_strike(price * (1.02 if opt_type == "call" else 0.98), step)
    entry_d     = date.fromisoformat(ts[:10])

    # Try live options chain first — real expiry, real strike, real mid price
    live = _live_chain(scan["ticker"], opt_type, target_k, entry_d)
    if live:
        strike      = live["strike"]
        val         = live["mid"]
        expiry_date = live["expiry"]
        sigma       = live["iv"] if live["iv"] > 0 else _hv(scan["ticker"])
        t_days      = live["dte_actual"]
        chain_src   = "live"
    else:
        # Fallback: Black-Scholes with standard monthly expiry
        sigma       = _hv(scan["ticker"])
        val         = _bs(price, target_k, DTE_TARGET / 365.0, sigma, opt_type)
        strike      = target_k
        expiry_date = _options_expiry(entry_d)
        t_days      = DTE_TARGET
        chain_src   = "synthetic"

    if val <= 0.05:
        return None

    cost = round(val * 100 * OPTION_CONTRACTS, 2)
    return {
        "id":                  f"{ts}_{scan['ticker']}_{trade_type}",
        "date_entered":        ts,
        "ticker":              scan["ticker"],
        "trade_type":          trade_type,
        "status":              "open",
        "opt_type":            opt_type,
        "strike":              strike,
        "t_days":              t_days,
        "expiry_date":         expiry_date,
        "sigma":               round(sigma, 4),
        "chain_source":        chain_src,
        "entry_stock_price":   round(price, 4),
        "entry_option_price":  round(val, 4),
        "cost_basis":          cost,
        "contracts":           OPTION_CONTRACTS,
        "current_option_price": round(val, 4),
        "pnl":                 0.0,
        "pnl_pct":             0.0,
        "date_closed":         None,
        "close_reason":        None,
        "days_held":           0,
        "mtf_score":           scan.get("mtf_score", 0),
        "rsi_entry":           round(scan.get("rsi", 50), 1),
    }


def _build_spread(scan: dict, trade_type: str, ts: str) -> Optional[dict]:
    price = scan.get("price", 0)
    if not price:
        return None
    step  = _strike_step(price)
    width = step * 2   # e.g. 5-wide on $200 stock, 2-wide on $60 stock

    if trade_type == "call_spread":
        long_k   = _round_strike(price * 1.01, step)
        short_k  = long_k + width
        opt_type = "call"
    else:  # put_spread
        long_k   = _round_strike(price * 0.99, step)
        short_k  = long_k - width
        opt_type = "put"

    # Try live chain on the near-ATM long leg — gives us real expiry + real IV
    entry_d = date.fromisoformat(ts[:10])
    live = _live_chain(scan["ticker"], opt_type, long_k, entry_d)
    if live:
        expiry_date = live["expiry"]
        t_days      = live["dte_actual"]
        sigma       = live["iv"] if live["iv"] > 0 else _hv(scan["ticker"])
        chain_src   = "live"
    else:
        expiry_date = _options_expiry(entry_d)
        t_days      = DTE_TARGET
        sigma       = _hv(scan["ticker"])
        chain_src   = "synthetic"

    T         = t_days / 365.0
    long_val  = _bs(price, long_k,  T, sigma, opt_type)
    short_val = _bs(price, short_k, T, sigma, opt_type)
    debit     = round(long_val - short_val, 4)

    if debit <= 0.05:
        return None

    max_gain = round(width - debit, 4)
    cost     = round(debit * 100 * OPTION_CONTRACTS, 2)

    return {
        "id":                f"{ts}_{scan['ticker']}_{trade_type}",
        "date_entered":      ts,
        "ticker":            scan["ticker"],
        "trade_type":        trade_type,
        "status":            "open",
        "opt_type":          opt_type,
        "long_strike":       long_k,
        "short_strike":      short_k,
        "spread_width":      width,
        "t_days":            t_days,
        "expiry_date":       expiry_date,
        "sigma":             round(sigma, 4),
        "chain_source":      chain_src,
        "entry_stock_price": round(price, 4),
        "entry_net_debit":   debit,
        "max_gain":          max_gain,
        "cost_basis":        cost,
        "contracts":         OPTION_CONTRACTS,
        "current_net_value": debit,
        "pnl":               0.0,
        "pnl_pct":           0.0,
        "date_closed":       None,
        "close_reason":      None,
        "days_held":         0,
        "mtf_score":         scan.get("mtf_score", 0),
        "rsi_entry":         round(scan.get("rsi", 50), 1),
    }


def _build_iron_condor(scan: dict, ts: str) -> Optional[dict]:
    price = scan.get("price", 0)
    if not price:
        return None
    step  = _strike_step(price)
    width = step * 2

    # Short strangle ≈10 % OTM, long wings one width further out
    sc_k = _round_strike(price * 1.08, step)
    lc_k = sc_k + width
    sp_k = _round_strike(price * 0.92, step)
    lp_k = sp_k - width

    # Use live chain on the short call leg — gets us real expiry + IV for all four legs
    entry_d = date.fromisoformat(ts[:10])
    live = _live_chain(scan["ticker"], "call", sc_k, entry_d)
    if live:
        expiry_date = live["expiry"]
        t_days      = live["dte_actual"]
        sigma       = live["iv"] if live["iv"] > 0 else _hv(scan["ticker"])
        chain_src   = "live"
    else:
        expiry_date = _options_expiry(entry_d)
        t_days      = DTE_TARGET
        sigma       = _hv(scan["ticker"])
        chain_src   = "synthetic"

    T  = t_days / 365.0
    sc = _bs(price, sc_k, T, sigma, "call")
    lc = _bs(price, lc_k, T, sigma, "call")
    sp = _bs(price, sp_k, T, sigma, "put")
    lp = _bs(price, lp_k, T, sigma, "put")

    credit = round((sc - lc) + (sp - lp), 4)
    if credit <= 0.05:
        return None

    max_loss = round(width - credit, 4)

    return {
        "id":                f"{ts}_{scan['ticker']}_iron_condor",
        "date_entered":      ts,
        "ticker":            scan["ticker"],
        "trade_type":        "iron_condor",
        "status":            "open",
        "long_call_k":       lc_k,
        "short_call_k":      sc_k,
        "short_put_k":       sp_k,
        "long_put_k":        lp_k,
        "spread_width":      width,
        "t_days":            t_days,
        "expiry_date":       expiry_date,
        "sigma":             round(sigma, 4),
        "chain_source":      chain_src,
        "entry_stock_price": round(price, 4),
        "entry_net_credit":  credit,
        "max_gain":          credit,
        "max_loss":          max_loss,
        "cost_basis":        round(max_loss * 100 * OPTION_CONTRACTS, 2),  # max risk
        "contracts":         OPTION_CONTRACTS,
        "current_net_value": credit,
        "pnl":               0.0,
        "pnl_pct":           0.0,
        "date_closed":       None,
        "close_reason":      None,
        "days_held":         0,
        "mtf_score":         scan.get("mtf_score", 0),
        "rsi_entry":         round(scan.get("rsi", 50), 1),
    }


def _build_credit_spread(scan: dict, trade_type: str, ts: str) -> Optional[dict]:
    """
    Build a credit spread — we SELL premium and collect income.
    bull_put_spread : sell put 3% below spot, buy put 6% below. Profit if stock stays up.
    bear_call_spread: sell call 3% above spot, buy call 6% above. Profit if stock stays flat/down.
    The IV premium works FOR us here — expensive options = bigger credit collected.
    """
    price = scan.get("price", 0)
    if not price:
        return None
    step = _strike_step(price)

    if trade_type == "bull_put_spread":
        short_k  = _round_strike(price * 0.97, step)   # sell 3% OTM put
        long_k   = _round_strike(price * 0.94, step)   # buy 6% OTM put (cap downside)
        opt_type = "put"
    else:  # bear_call_spread
        short_k  = _round_strike(price * 1.03, step)   # sell 3% OTM call
        long_k   = _round_strike(price * 1.06, step)   # buy 6% OTM call (cap upside loss)
        opt_type = "call"

    # Use live chain on the short leg — we're selling that one, so its pricing matters most
    entry_d = date.fromisoformat(ts[:10])
    live = _live_chain(scan["ticker"], opt_type, short_k, entry_d)
    if live:
        expiry_date = live["expiry"]
        t_days      = live["dte_actual"]
        sigma       = live["iv"] if live["iv"] > 0 else _hv(scan["ticker"])
        chain_src   = "live"
    else:
        expiry_date = _options_expiry(entry_d)
        t_days      = DTE_TARGET
        sigma       = _hv(scan["ticker"])
        chain_src   = "synthetic"

    T         = t_days / 365.0
    short_val = _bs(price, short_k, T, sigma, opt_type)
    long_val  = _bs(price, long_k,  T, sigma, opt_type)
    credit    = round(short_val - long_val, 4)

    if credit <= 0.05:
        return None

    spread_width = round(abs(short_k - long_k), 2)
    max_loss     = round(spread_width - credit, 4)

    return {
        "id":                 f"{ts}_{scan['ticker']}_{trade_type}",
        "date_entered":       ts,
        "ticker":             scan["ticker"],
        "trade_type":         trade_type,
        "status":             "open",
        "opt_type":           opt_type,
        "short_strike":       short_k,
        "long_strike":        long_k,
        "spread_width":       spread_width,
        "t_days":             t_days,
        "expiry_date":        expiry_date,
        "sigma":              round(sigma, 4),
        "chain_source":       chain_src,
        "entry_stock_price":  round(price, 4),
        "entry_net_credit":   credit,
        "max_gain":           credit,
        "max_loss":           max_loss,
        "cost_basis":         round(max_loss * 100 * OPTION_CONTRACTS, 2),
        "contracts":          OPTION_CONTRACTS,
        "current_cost_to_close": credit,   # starts at full credit (cost to unwind = full credit)
        "pnl":                0.0,
        "pnl_pct":            0.0,
        "date_closed":        None,
        "close_reason":       None,
        "days_held":          0,
        "mtf_score":          scan.get("mtf_score", 0),
        "rsi_entry":          round(scan.get("rsi", 50), 1),
    }


def _build_covered_call(scan: dict, ts: str) -> Optional[dict]:
    """
    Sell a 30 DTE call 3% OTM for income. Works best on stocks with high IV
    or flat/mildly bullish expectations — collect theta decay every day.
    """
    price = scan.get("price", 0)
    if not price:
        return None
    step         = _strike_step(price)
    target_k     = _round_strike(price * 1.03, step)   # 3% OTM
    entry_d      = date.fromisoformat(ts[:10])

    live = _live_chain(scan["ticker"], "call", target_k, entry_d)
    if live:
        strike      = live["strike"]
        premium     = live["mid"]
        expiry_date = live["expiry"]
        sigma       = live["iv"] if live["iv"] > 0 else _hv(scan["ticker"])
        t_days      = live["dte_actual"]
        chain_src   = "live"
    else:
        sigma       = _hv(scan["ticker"])
        premium     = _bs(price, target_k, DTE_TARGET / 365.0, sigma, "call")
        strike      = target_k
        expiry_date = _options_expiry(entry_d)
        t_days      = DTE_TARGET
        chain_src   = "synthetic"

    if premium <= 0.05:
        return None

    return {
        "id":                  f"{ts}_{scan['ticker']}_covered_call",
        "date_entered":        ts,
        "ticker":              scan["ticker"],
        "trade_type":          "covered_call",
        "status":              "open",
        "opt_type":            "call",
        "strike":              strike,
        "t_days":              t_days,
        "expiry_date":         expiry_date,
        "sigma":               round(sigma, 4),
        "chain_source":        chain_src,
        "entry_stock_price":   round(price, 4),
        "entry_option_price":  round(premium, 4),
        "premium_collected":   round(premium * 100 * OPTION_CONTRACTS, 2),
        "cost_basis":          round(premium * 100 * OPTION_CONTRACTS, 2),
        "contracts":           OPTION_CONTRACTS,
        "current_option_price": round(premium, 4),
        "pnl":                 0.0,
        "pnl_pct":             0.0,
        "date_closed":         None,
        "close_reason":        None,
        "days_held":           0,
        "mtf_score":           scan.get("mtf_score", 0),
        "rsi_entry":           round(scan.get("rsi", 50), 1),
    }


def _build_trade(scan: dict, trade_type: str, ts: str) -> Optional[dict]:
    if trade_type in ("stock_long", "stock_short"):
        return _build_stock(scan, trade_type, ts)
    if trade_type in ("long_call", "long_put"):
        return _build_option(scan, trade_type, ts)
    if trade_type in ("call_spread", "put_spread"):
        return _build_spread(scan, trade_type, ts)
    if trade_type == "iron_condor":
        return _build_iron_condor(scan, ts)
    if trade_type in ("bull_put_spread", "bear_call_spread"):
        return _build_credit_spread(scan, trade_type, ts)
    if trade_type == "covered_call":
        return _build_covered_call(scan, ts)
    return None


# ── Mark-to-market ─────────────────────────────────────────────────────────────

def _days_since(date_str: str) -> int:
    try:
        entry = datetime.fromisoformat(date_str[:19]).date()
        return max((date.today() - entry).days, 0)
    except Exception:
        return 0


def mark_trade(trade: dict, price: Optional[float] = None) -> dict:
    """Re-price an open trade. Pass price to avoid redundant yfinance calls."""
    if trade["status"] != "open":
        return trade

    t = dict(trade)
    if price is None:
        price = _live_price(t["ticker"])
    if not price:
        return t

    tt        = t["trade_type"]
    days_held = _days_since(t["date_entered"])
    t["days_held"] = days_held

    try:
        if tt in ("stock_long", "stock_short"):
            sign = 1 if tt == "stock_long" else -1
            t["current_price"] = round(price, 4)
            raw  = (price - t["entry_price"]) * t["shares"] * sign
            t["pnl"]     = round(raw, 2)
            t["pnl_pct"] = round(raw / t["cost_basis"], 4) if t["cost_basis"] else 0.0

        elif tt in ("long_call", "long_put"):
            T_rem = max((t["t_days"] - days_held) / 365.0, 0.001)
            cur   = _bs(price, t["strike"], T_rem, t["sigma"], t["opt_type"])
            t["current_option_price"] = round(cur, 4)
            raw  = (cur - t["entry_option_price"]) * 100 * t["contracts"]
            t["pnl"]     = round(raw, 2)
            t["pnl_pct"] = round(raw / t["cost_basis"], 4) if t["cost_basis"] else 0.0
            # Greeks — long option (paying theta, earning on correct direction)
            g = _greeks(price, t["strike"], T_rem, t["sigma"], t["opt_type"])
            t["delta"]           = g["delta"]
            t["theta"]           = g["theta"]
            t["gamma"]           = g["gamma"]
            t["vega"]            = g["vega"]
            t["theta_dollar_day"] = round(g["theta"] * 100 * t["contracts"], 2)
            # Moneyness: how far OTM (positive = OTM, negative = ITM)
            if tt == "long_call":
                t["moneyness_pct"] = round((t["strike"] - price) / price * 100, 1)
            else:
                t["moneyness_pct"] = round((price - t["strike"]) / price * 100, 1)

        elif tt in ("call_spread", "put_spread"):
            T_rem  = max((t["t_days"] - days_held) / 365.0, 0.001)
            lv     = _bs(price, t["long_strike"],  T_rem, t["sigma"], t["opt_type"])
            sv     = _bs(price, t["short_strike"], T_rem, t["sigma"], t["opt_type"])
            net    = lv - sv
            t["current_net_value"] = round(net, 4)
            raw  = (net - t["entry_net_debit"]) * 100 * t["contracts"]
            t["pnl"]     = round(raw, 2)
            t["pnl_pct"] = round(raw / t["cost_basis"], 4) if t["cost_basis"] else 0.0
            # Net Greeks: long leg minus short leg (we're long the spread)
            g_l = _greeks(price, t["long_strike"],  T_rem, t["sigma"], t["opt_type"])
            g_s = _greeks(price, t["short_strike"], T_rem, t["sigma"], t["opt_type"])
            t["delta"]            = round(g_l["delta"] - g_s["delta"], 4)
            t["theta"]            = round(g_l["theta"] - g_s["theta"], 4)
            t["vega"]             = round(g_l["vega"]  - g_s["vega"],  4)
            t["theta_dollar_day"] = round(t["theta"] * 100 * t["contracts"], 2)

        elif tt == "iron_condor":
            T_rem = max((t["t_days"] - days_held) / 365.0, 0.001)
            sc = _bs(price, t["short_call_k"], T_rem, t["sigma"], "call")
            lc = _bs(price, t["long_call_k"],  T_rem, t["sigma"], "call")
            sp = _bs(price, t["short_put_k"],  T_rem, t["sigma"], "put")
            lp = _bs(price, t["long_put_k"],   T_rem, t["sigma"], "put")
            cur_net = (sc - lc) + (sp - lp)
            t["current_net_value"] = round(cur_net, 4)
            raw  = (t["entry_net_credit"] - cur_net) * 100 * t["contracts"]
            ml   = t.get("max_loss", 1) * 100 or 100
            t["pnl"]     = round(raw, 2)
            t["pnl_pct"] = round(raw / ml, 4)
            # Net Greeks: SHORT sc, LONG lc, SHORT sp, LONG lp
            g_sc = _greeks(price, t["short_call_k"], T_rem, t["sigma"], "call")
            g_lc = _greeks(price, t["long_call_k"],  T_rem, t["sigma"], "call")
            g_sp = _greeks(price, t["short_put_k"],  T_rem, t["sigma"], "put")
            g_lp = _greeks(price, t["long_put_k"],   T_rem, t["sigma"], "put")
            net_theta = (-g_sc["theta"] + g_lc["theta"]
                         - g_sp["theta"] + g_lp["theta"])
            net_delta = (-g_sc["delta"] + g_lc["delta"]
                         - g_sp["delta"] + g_lp["delta"])
            t["delta"]            = round(net_delta, 4)   # near-zero for condor
            t["theta"]            = round(net_theta, 4)   # positive: collecting theta
            t["theta_dollar_day"] = round(net_theta * 100 * t["contracts"], 2)

        elif tt in ("bull_put_spread", "bear_call_spread"):
            T_rem    = max((t["t_days"] - days_held) / 365.0, 0.001)
            sv       = _bs(price, t["short_strike"], T_rem, t["sigma"], t["opt_type"])
            lv       = _bs(price, t["long_strike"],  T_rem, t["sigma"], t["opt_type"])
            cost_now = max(sv - lv, 0)
            t["current_cost_to_close"] = round(cost_now, 4)
            credit = t.get("entry_net_credit", 0)
            raw    = (credit - cost_now) * 100 * t["contracts"]
            ml     = t.get("max_loss", 1) * 100 or 100
            t["pnl"]     = round(raw, 2)
            t["pnl_pct"] = round(raw / ml, 4)
            # Net Greeks: SHORT short_strike, LONG long_strike
            g_s = _greeks(price, t["short_strike"], T_rem, t["sigma"], t["opt_type"])
            g_l = _greeks(price, t["long_strike"],  T_rem, t["sigma"], t["opt_type"])
            net_delta = -g_s["delta"] + g_l["delta"]   # positive for bull_put, negative for bear_call
            net_theta = -g_s["theta"] + g_l["theta"]   # positive: credit spread collects theta
            t["delta"]            = round(net_delta, 4)
            t["theta"]            = round(net_theta, 4)
            t["theta_dollar_day"] = round(net_theta * 100 * t["contracts"], 2)
            # % of credit already captured
            t["credit_captured_pct"] = round((1 - cost_now / credit) * 100, 1) if credit else 0

        elif tt == "covered_call":
            T_rem = max((t["t_days"] - days_held) / 365.0, 0.001)
            cur   = _bs(price, t["strike"], T_rem, t["sigma"], "call")
            t["current_option_price"] = round(cur, 4)
            entry_prem = t.get("entry_option_price", 0)
            raw  = (entry_prem - cur) * 100 * t["contracts"]
            cb   = t.get("cost_basis", 1) or 1
            t["pnl"]     = round(raw, 2)
            t["pnl_pct"] = round(raw / cb, 4)
            # Greeks: SHORT the call → flip signs
            g = _greeks(price, t["strike"], T_rem, t["sigma"], "call")
            t["delta"]            = round(-g["delta"], 4)   # negative: short call
            t["theta"]            = round(-g["theta"], 4)   # positive: collecting theta
            t["theta_dollar_day"] = round(-g["theta"] * 100 * t["contracts"], 2)
            # How close is the stock to being called away?
            t["moneyness_pct"]    = round((t["strike"] - price) / price * 100, 1)

    except Exception:
        pass

    return t


def _should_close(t: dict) -> Optional[str]:
    """
    Determines if a trade should be closed based on rules.

    Stocks      → fixed profit/stop (signal confirmed these work at 50.8 % WR).
    Options     → AI agent decisions only (see agent_exit_review).
                  Only the BACKSTOP_OPT safety net and MAX_HOLD_OPT apply here.
    Iron condors→ credit-trade rules (50 % of credit earned = close).
    """
    days = t.get("days_held", 0)
    pct  = t.get("pnl_pct",  0.0)
    pnl  = t.get("pnl",      0.0)
    tt   = t["trade_type"]

    # ── Stocks: fixed exits (work well in backtest) ───────────────────────────
    if tt in ("stock_long", "stock_short"):
        if days >= MAX_HOLD_STK:       return "max_hold"
        if pct >= STK_PROFIT:          return "profit_target"
        if pct <= STK_STOP:            return "stop_loss"

    # ── Long options / spreads: agent-driven, safety net only ─────────────────
    elif tt in ("long_call", "long_put", "call_spread", "put_spread"):
        if days >= MAX_HOLD_OPT:       return "max_hold"       # approaching expiry
        if pct <= BACKSTOP_OPT:        return "backstop_stop"  # catastrophic protection

    # ── Iron condors: credit-trade rules (time decay is the edge here) ────────
    elif tt == "iron_condor":
        if days >= MAX_HOLD_OPT:       return "max_hold"
        credit_usd = t.get("entry_net_credit", 0) * 100
        if credit_usd > 0:
            if pnl >= credit_usd * IC_PROFIT:  return "profit_target"
            if pnl <= -credit_usd * IC_STOP:   return "stop_loss"

    # ── Credit spreads: rules-based (credit trades always use fixed rules) ────
    elif tt in ("bull_put_spread", "bear_call_spread"):
        if days >= MAX_HOLD_OPT:       return "max_hold"
        credit    = t.get("entry_net_credit", 0)
        cost_now  = t.get("current_cost_to_close", credit)
        if credit > 0:
            if cost_now <= credit * 0.25:  return "profit_target"  # kept 75% of credit
            if cost_now >= credit * 2.0:   return "stop_loss"       # spread doubled against us

    # ── Covered calls: rules-based (premium decay trade) ─────────────────────
    elif tt == "covered_call":
        if days >= MAX_HOLD_OPT:       return "max_hold"
        entry_prem = t.get("entry_option_price", 0)
        cur_prem   = t.get("current_option_price", entry_prem)
        if entry_prem > 0:
            if cur_prem <= entry_prem * 0.20:  return "profit_target"  # kept 80% of premium
            if cur_prem >= entry_prem * 2.0:   return "stop_loss"      # stock blew through strike

    return None


def agent_exit_review(open_trades: list) -> dict:
    """
    Ask Claude (Haiku) to review all open options/spreads and decide HOLD or CLOSE.
    Returns {trade_id: close_reason} for positions the agent wants to close.
    Runs at 4:05 PM ET after mark_trade has refreshed P&L and Greeks.

    Greeks give the agent real options intuition:
    - theta_dollar_day: how many $ you're bleeding (or earning) per day from time decay
    - delta: directional exposure — low delta on a long call means deep OTM, stock barely moves it
    - guaranteed_theta_cost: total theta bleed if held to expiry (dte × theta/day)
    - credit_captured_pct: for credit spreads, how much of the max profit is already in the bag
    """
    OPT_TYPES = (
        "long_call", "long_put",
        "call_spread", "put_spread",
        "bull_put_spread", "bear_call_spread",
        "covered_call", "iron_condor",
    )
    option_trades = [t for t in open_trades if t.get("trade_type") in OPT_TYPES]
    if not option_trades:
        return {}

    try:
        from anthropic import Anthropic
        client = Anthropic()

        positions = []
        for t in option_trades:
            dte  = max(t.get("t_days", 30) - t.get("days_held", 0), 0)
            tpd  = t.get("theta_dollar_day", 0)
            pos  = {
                "id":                  t["id"],
                "ticker":              t["ticker"],
                "type":                t["trade_type"],
                "days_held":           t.get("days_held", 0),
                "dte_remaining":       dte,
                "pnl_pct":             round(t.get("pnl_pct", 0) * 100, 1),
                "pnl_usd":             round(t.get("pnl", 0), 2),
                "delta":               t.get("delta", 0),
                "theta_per_day_usd":   round(tpd, 2),
                "guaranteed_theta_cost": round(tpd * dte, 2),  # total bleed/income if held to expiry
                "mtf_score":           t.get("mtf_score", 0),
                "rsi_at_entry":        t.get("rsi_entry", 50),
            }
            # Type-specific extras
            if t["trade_type"] in ("long_call", "long_put"):
                pos["moneyness_pct"] = t.get("moneyness_pct", 0)
                pos["strike"]        = t.get("strike")
                pos["vega"]          = t.get("vega", 0)
            elif t["trade_type"] in ("bull_put_spread", "bear_call_spread"):
                pos["credit_captured_pct"] = t.get("credit_captured_pct", 0)
                pos["short_strike"]        = t.get("short_strike")
                pos["long_strike"]         = t.get("long_strike")
            elif t["trade_type"] in ("call_spread", "put_spread"):
                pos["long_strike"]  = t.get("long_strike")
                pos["short_strike"] = t.get("short_strike")
            elif t["trade_type"] == "covered_call":
                pos["strike"]        = t.get("strike")
                pos["moneyness_pct"] = t.get("moneyness_pct", 0)  # +% = OTM (safe), 0% = at strike
            elif t["trade_type"] == "iron_condor":
                pos["short_put_k"]  = t.get("short_put_k")
                pos["short_call_k"] = t.get("short_call_k")
            positions.append(pos)

        prompt = f"""You are a professional options portfolio manager. Review the positions below and decide HOLD or CLOSE for each.

KEY METRICS EXPLAINED:
- theta_per_day_usd: daily P&L from time decay alone
  • Negative = you're PAYING theta (long options — time working against you)
  • Positive = you're EARNING theta (credit spreads, covered calls — time working for you)
- guaranteed_theta_cost: total theta impact if held to expiry (theta/day × DTE left)
  • A long call losing $8/day with 6 DTE = $48 of guaranteed theta loss coming
- delta: how much the position moves per $1 stock move (× 100 shares)
  • Long call Δ=0.12 → stock moves $1, option moves $0.12 → deep OTM, needs big move
  • Long call Δ=0.65 → stock moves $1, option moves $0.65 → nearly ITM, working well
- credit_captured_pct: for credit spreads, % of max profit already earned
  • 70%+ → consider closing, you've captured most of the premium

DECISION FRAMEWORK (use judgment, not rigid rules):
- DTE < 7: CLOSE — theta decay accelerates, risk/reward degrades sharply
- Long options, delta < 0.15, losing money: CLOSE — deep OTM, need miracle move
- Long options: |guaranteed_theta_cost| > current position value: CLOSE — theta will eat you alive
- Long options in clear profit (>30%) + delta fading: CLOSE — take gains before theta takes them
- Credit spreads, credit_captured_pct > 75%: CLOSE — most of the gain is made, don't risk reversal
- Covered calls, moneyness < 1%: careful — stock near strike, may get called away soon
- MTF 3/3 conviction with <10 days held, slightly red: HOLD — good signal, give it time
- Any position at >21 days: CLOSE unconditionally

OPEN POSITIONS:
{json.dumps(positions, indent=2)}

Respond ONLY with valid JSON in this exact format (no markdown, no explanation outside JSON):
{{
  "decisions": [
    {{"id": "trade_id", "action": "HOLD", "reason": "one sentence"}},
    {{"id": "trade_id", "action": "CLOSE", "reason": "one sentence"}}
  ]
}}"""

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:])
            text = text.rsplit("```", 1)[0].strip()

        result   = json.loads(text)
        to_close = {}
        for d in result.get("decisions", []):
            if d.get("action") == "CLOSE":
                to_close[d["id"]] = d.get("reason", "agent_decision")

        held  = len(option_trades) - len(to_close)
        print(f"[AgentExit] Reviewed {len(option_trades)} options → "
              f"{len(to_close)} CLOSE, {held} HOLD")
        for tid, reason in to_close.items():
            print(f"[AgentExit]  CLOSE {tid[:40]}: {reason}")
        return to_close

    except Exception as e:
        print(f"[AgentExit] Review failed: {e}")
        return {}


# ── Daily generate ─────────────────────────────────────────────────────────────

def generate_daily_trades(n: int = 10) -> list:
    """
    Generate n paper trades using the ConquestAgentSystem (6 parallel AI agents).
    Falls back to signal-based generation for any slots the agent system can't fill.
    Safe to call multiple times — skips if today's batch already exists.
    Returns list of new trades added (empty if already ran today).
    """
    if not _is_trading_day():
        print("[PaperTrader] Skipping trade generation — markets are closed (weekend).")
        return []

    from concurrent.futures import ThreadPoolExecutor, as_completed
    sys.path.insert(0, APP_DIR)

    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    all_trades = load_trades()

    # Already ran today?
    already_today = [t for t in all_trades
                     if t.get("date_entered", "").startswith(today_str)]
    if len(already_today) >= n:
        print(f"[PaperTrader] Already have {len(already_today)} trades for {today_str}.")
        return []

    used_tickers = {t["ticker"] for t in already_today}
    ts           = datetime.now(ET).strftime("%Y-%m-%dT%H:%M")
    new_trades   = []
    type_counts  = {tt: 0 for tt in _TYPE_WEIGHTS}

    # ── Pre-screen: expand from 20 to 120-ticker universe, pick top 40 ──────────
    try:
        from universe_screener import pre_screen
        scan_universe = pre_screen(n=40)
    except Exception as _ps_err:
        print(f"[PaperTrader] Pre-screener unavailable ({_ps_err}), using base universe.")
        scan_universe = PAPER_UNIVERSE

    # scans always defined — used later by reasoning generation regardless of which path ran
    scans: list = []

    # ── Primary path: ConquestAgentSystem (6-agent swarm) ─────────────────────
    try:
        from conquest_agents import get_agent_system
        print(f"[PaperTrader] Launching 6-agent swarm across {len(scan_universe)} candidates …")
        agent_trades = get_agent_system().generate_trades(
            scan_universe, n=n, existing_tickers=used_tickers
        )
        new_trades   = agent_trades
        for t in new_trades:
            type_counts[t["trade_type"]] = type_counts.get(t["trade_type"], 0) + 1
            used_tickers.add(t["ticker"])
            # Build a minimal scan record so reasoning generation has context
            # for agent trades (full scan lives inside TickerData, not on the trade dict)
            scans.append({
                "ticker":    t["ticker"],
                "price":     t.get("entry_stock_price", 0),
                "mtf_score": t.get("mtf_score", 0),
                "rsi":       t.get("rsi_entry", 50),
                "adx":       t.get("adx_entry", 0),
            })
        print(f"[PaperTrader] Agent system produced {len(new_trades)} high-conviction trades.")
    except Exception as e:
        print(f"[PaperTrader] Agent system unavailable ({e}), using signal scanner.")
        new_trades = []

    # ── Fallback / fill: signal-based for any remaining slots ─────────────────
    if len(new_trades) < n:
        remaining_needed = n - len(new_trades)
        print(f"[PaperTrader] Filling {remaining_needed} slot(s) via signal scanner …")
        from alerts.scanner import scan_ticker

        fallback_scans = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(scan_ticker, t): t
                    for t in scan_universe if t not in used_tickers}
            for fut in as_completed(futs):
                r = fut.result()
                if not r.get("error") and (r.get("price") or 0) > 0:
                    fallback_scans.append(r)

        fallback_scans.sort(key=lambda s: (
            -int(s.get("sqz_fired",    False)),
            -int(s.get("entry_signal", False)),
            -s.get("mtf_score", 0),
        ))

        # First pass: top-signal tickers
        for scan in fallback_scans:
            if len(new_trades) >= n:
                break
            if scan["ticker"] in used_tickers:
                continue

            trade_type = _assign_trade_type(scan)

            if type_counts.get(trade_type, 0) >= 3:
                pool_candidates = [tt for tt in _TYPE_WEIGHTS
                                   if type_counts.get(tt, 0) < 3]
                if not pool_candidates:
                    break
                trade_type = min(pool_candidates,
                                 key=lambda tt: type_counts.get(tt, 0))

            trade = _build_trade(scan, trade_type, ts)
            if trade:
                trade["adx_entry"] = round(scan.get("adx", 0), 1)
                new_trades.append(trade)
                type_counts[trade_type] = type_counts.get(trade_type, 0) + 1
                used_tickers.add(scan["ticker"])

        # Second pass: iron condors on leftovers
        if len(new_trades) < n:
            remaining = [s for s in fallback_scans if s["ticker"] not in used_tickers]
            fallback_types = ["iron_condor", "call_spread", "put_spread",
                              "long_call", "long_put", "stock_long"]
            fi = 0
            for scan in remaining:
                if len(new_trades) >= n:
                    break
                tt    = fallback_types[fi % len(fallback_types)]
                fi   += 1
                trade = _build_trade(scan, tt, ts)
                if trade:
                    new_trades.append(trade)
                    used_tickers.add(scan["ticker"])

        # Merge fallback scans into master list for reasoning generation
        scans.extend(fallback_scans)

    # ── Generate entry reasoning for every new trade ──────────────────────────
    if new_trades:
        try:
            from conquest_brain import generate_trade_reasonings
            # Match each trade back to its scan result
            scan_map = {s["ticker"]: s for s in scans}
            trades_and_scans = [
                (t, scan_map.get(t["ticker"], {}))
                for t in new_trades
            ]
            reasonings = generate_trade_reasonings(trades_and_scans)
            for t in new_trades:
                t["reasoning"] = reasonings.get(t["id"], "Reasoning unavailable.")
            print(f"[PaperTrader] Entry reasoning generated for {len(reasonings)} trades.")
        except Exception as e:
            print(f"[PaperTrader] Reasoning generation failed: {e}")
            for t in new_trades:
                t.setdefault("reasoning", "Reasoning unavailable.")

    all_trades.extend(new_trades)
    save_trades(all_trades)

    # Submit stock trades to Alpaca (options stay simulated)
    try:
        from broker import execute_trade, broker_available
        if broker_available():
            stock_trades = [t for t in new_trades
                            if t["trade_type"] in ("stock_long", "stock_short")]
            for t in stock_trades:
                updated = execute_trade(t)
                # Update broker fields in saved trades
                idx = next((i for i, s in enumerate(all_trades)
                            if s.get("id") == t.get("id")), None)
                if idx is not None:
                    all_trades[idx].update({
                        k: updated[k] for k in
                        ("broker_order_id", "broker_status", "broker_mode", "broker_note")
                        if k in updated
                    })
            if stock_trades:
                save_trades(all_trades)
                print(f"[PaperTrader] Submitted {len(stock_trades)} stock orders to Alpaca.")
        else:
            print("[PaperTrader] Alpaca not configured — all trades simulated.")
    except Exception as e:
        print(f"[PaperTrader] Broker execution skipped: {e}")

    # Log each new trade to Notion Trade Journal
    try:
        from notion_journal import log_trade_open
        logged = sum(1 for t in new_trades if log_trade_open(t))
        if logged:
            print(f"[PaperTrader] Logged {logged} trades to Notion.")
    except Exception as e:
        print(f"[PaperTrader] Notion open-logging skipped: {e}")

    summary = {}
    for t in new_trades:
        summary[t["trade_type"]] = summary.get(t["trade_type"], 0) + 1

    print(f"[PaperTrader] Generated {len(new_trades)} trades: {summary}")
    return new_trades


# ── Daily close ────────────────────────────────────────────────────────────────

def _is_trading_day(dt=None) -> bool:
    """Return True only if dt (default: now ET) is a weekday (Mon–Fri)."""
    if dt is None:
        dt = datetime.now(ET)
    return dt.weekday() < 5  # 0=Mon … 4=Fri, 5=Sat, 6=Sun


def run_daily_close() -> dict:
    """
    Mark every open trade to market, close any that hit stop/target/max-hold.
    Call at market close (4:05 PM ET).
    """
    if not _is_trading_day():
        print("[PaperTrader] Skipping close run — markets are closed (weekend).")
        trades = load_trades()
        open_count = sum(1 for t in trades if t.get("status") == "open")
        return {"total_open": open_count, "closed": 0, "still_open": open_count,
                "skipped": True, "reason": "weekend"}

    trades = load_trades()
    open_trades = [t for t in trades if t.get("status") == "open"]

    # Batch price fetch — one yfinance call per unique ticker
    import yfinance as yf
    tickers = list({t["ticker"] for t in open_trades})
    prices  = {}
    if tickers:
        try:
            data = yf.download(tickers, period="1d", interval="1m",
                               progress=False, auto_adjust=True)
            for tk in tickers:
                try:
                    prices[tk] = float(yf.Ticker(tk).fast_info["lastPrice"])
                except Exception:
                    pass
        except Exception:
            pass

    closed_count = 0
    now_str      = datetime.now(ET).strftime("%Y-%m-%dT%H:%M")

    # ── Mark all open trades to current prices first ──────────────────────────
    for i, trade in enumerate(trades):
        if trade["status"] != "open":
            continue
        price     = prices.get(trade["ticker"])
        trades[i] = mark_trade(trade, price=price)

    # ── Agent exit review for option positions ────────────────────────────────
    open_options = [t for t in trades
                    if t.get("status") == "open"
                    and t.get("trade_type") in
                    ("long_call", "long_put", "call_spread", "put_spread")]
    agent_closes = agent_exit_review(open_options)  # {trade_id: reason}

    for i, trade in enumerate(trades):
        if trade["status"] != "open":
            continue
        reason = _should_close(trades[i])

        # Merge agent decision: if the agent wants to close and rules didn't, use agent
        if not reason and trades[i].get("id") in agent_closes:
            reason = "agent_decision"
            trades[i]["agent_close_reason"] = agent_closes[trades[i]["id"]]
        if reason:
            trades[i]["status"]       = "closed"
            trades[i]["date_closed"]  = now_str
            trades[i]["close_reason"] = reason
            # Generate close reasoning
            try:
                from conquest_brain import generate_close_reasoning
                trades[i]["close_reasoning"] = generate_close_reasoning(
                    trades[i], reason, price or 0
                )
            except Exception:
                trades[i]["close_reasoning"] = "Reasoning unavailable."
            # Update agent weights so the swarm learns from this outcome
            try:
                from conquest_agents import get_agent_system
                get_agent_system().update_weights_from_trade(trades[i])
            except Exception as _aw_err:
                pass   # weight learning is best-effort
            closed_count += 1

    save_trades(trades)

    # Log all closed trades to Notion
    try:
        from notion_journal import log_trade_close
        closed_trades = [t for t in trades if t.get("status") == "closed"
                         and t.get("date_closed", "").startswith(now_str[:10])]
        for t in closed_trades:
            log_trade_close(t)
        if closed_trades:
            print(f"[PaperTrader] Notion close-log: {len(closed_trades)} trades updated.")
    except Exception as e:
        print(f"[PaperTrader] Notion close-logging skipped: {e}")
    result = {
        "total_open":  len(open_trades),
        "closed":      closed_count,
        "still_open":  len(open_trades) - closed_count,
    }
    print(f"[PaperTrader] Close run: {result}")
    return result


# ── Statistics ─────────────────────────────────────────────────────────────────

def get_paper_stats() -> dict:
    """Return a comprehensive stats dict for the web dashboard and Discord."""
    trades  = load_trades()

    # Backfill expiry_date for older trades that predate the field, and fix any
    # raw DTE dates that landed on a weekend (use standard monthly expiry instead)
    for t in trades:
        entry_d = t.get("date_entered", "")[:10]
        t_days  = t.get("t_days")
        if entry_d and t_days:
            entry = date.fromisoformat(entry_d)
            proper = _options_expiry(entry, t_days)
            if "expiry_date" not in t or date.fromisoformat(t["expiry_date"]).weekday() >= 5:
                t["expiry_date"] = proper

    closed  = [t for t in trades if t.get("status") == "closed"]
    open_   = [t for t in trades if t.get("status") == "open"]

    base = {
        "total_trades": len(trades),
        "open_count":   len(open_),
        "closed_count": len(closed),
        "open_trades":  sorted(open_,   key=lambda t: t.get("pnl", 0), reverse=True),
        "closed_trades": sorted(closed, key=lambda t: t.get("date_closed") or "", reverse=True),
    }

    if not closed:
        return {**base,
                "win_rate": 0, "total_pnl": 0, "avg_pnl": 0,
                "avg_hold": 0, "best_trade": None, "worst_trade": None,
                "by_type": {}, "by_ticker": {},
                "sharpe": None, "profit_factor": None, "cum_pnl": []}

    wins      = [t for t in closed if t.get("pnl", 0) > 0]
    total_pnl = round(sum(t.get("pnl", 0) for t in closed), 2)
    avg_pnl   = round(total_pnl / len(closed), 2)
    avg_hold  = round(sum(t.get("days_held", 0) for t in closed) / len(closed), 1)
    best      = max(closed, key=lambda t: t.get("pnl", 0))
    worst     = min(closed, key=lambda t: t.get("pnl", 0))

    # ── By trade type ──────────────────────────────────────────────────────────
    by_type: dict = {}
    for t in closed:
        tt = t["trade_type"]
        if tt not in by_type:
            by_type[tt] = {"count": 0, "wins": 0, "total_pnl": 0.0}
        by_type[tt]["count"]     += 1
        by_type[tt]["total_pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            by_type[tt]["wins"] += 1
    for tt, d in by_type.items():
        d["win_rate"] = round(d["wins"] / d["count"], 3)
        d["avg_pnl"]  = round(d["total_pnl"] / d["count"], 2)
        d["total_pnl"] = round(d["total_pnl"], 2)

    # ── By ticker ──────────────────────────────────────────────────────────────
    by_ticker: dict = {}
    for t in closed:
        tk = t["ticker"]
        if tk not in by_ticker:
            by_ticker[tk] = {"count": 0, "wins": 0, "total_pnl": 0.0}
        by_ticker[tk]["count"]     += 1
        by_ticker[tk]["total_pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            by_ticker[tk]["wins"] += 1
    for tk, d in by_ticker.items():
        d["win_rate"]   = round(d["wins"] / d["count"], 3)
        d["avg_pnl"]    = round(d["total_pnl"] / d["count"], 2)
        d["total_pnl"]  = round(d["total_pnl"], 2)

    # ── Sharpe (annualised from per-trade P&L) ────────────────────────────────
    sharpe = None
    try:
        import numpy as np
        from collections import defaultdict
        daily_pnl: dict = defaultdict(float)
        for t in closed:
            d = (t.get("date_closed") or t.get("date_entered", ""))[:10]
            daily_pnl[d] += t.get("pnl", 0)
        vals = list(daily_pnl.values())
        if len(vals) > 1:
            mean_r = float(np.mean(vals))
            std_r  = float(np.std(vals, ddof=1))
            sharpe = round(mean_r / std_r * (252 ** 0.5), 2) if std_r > 0 else 0.0
    except Exception:
        pass

    # ── Profit factor ──────────────────────────────────────────────────────────
    gross_win  = sum(t.get("pnl", 0) for t in closed if t.get("pnl", 0) > 0)
    gross_loss = abs(sum(t.get("pnl", 0) for t in closed if t.get("pnl", 0) < 0))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    # ── Cumulative P&L curve ───────────────────────────────────────────────────
    sorted_closed = sorted(closed,
                           key=lambda t: t.get("date_closed") or t.get("date_entered", ""))
    cum_pnl, running = [], 0.0
    for t in sorted_closed:
        running += t.get("pnl", 0)
        cum_pnl.append({
            "date":    (t.get("date_closed") or t.get("date_entered", ""))[:10],
            "cum_pnl": round(running, 2),
            "ticker":  t["ticker"],
        })

    # ── Close-reason breakdown ─────────────────────────────────────────────────
    reason_counts: dict = {}
    for t in closed:
        r = t.get("close_reason", "unknown")
        reason_counts[r] = reason_counts.get(r, 0) + 1

    return {
        **base,
        "win_rate":      round(len(wins) / len(closed), 3),
        "total_pnl":     total_pnl,
        "avg_pnl":       avg_pnl,
        "avg_hold":      avg_hold,
        "best_trade":    best,
        "worst_trade":   worst,
        "by_type":       by_type,
        "by_ticker":     by_ticker,
        "sharpe":        sharpe,
        "profit_factor": profit_factor,
        "cum_pnl":       cum_pnl,
        "reason_counts": reason_counts,
    }


# ── CLI entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.path.insert(0, APP_DIR)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "generate"

    if cmd == "generate":
        trades = generate_daily_trades(10)
        if trades:
            print(f"\nNew trades:")
            for t in trades:
                print(f"  {t['trade_type']:15s}  {t['ticker']:6s}  cost=${t.get('cost_basis',0):.2f}")
        else:
            print("No new trades added (already ran today or no data).")

    elif cmd == "close":
        result = run_daily_close()
        print(f"\nClose result: {result}")

    elif cmd == "stats":
        s = get_paper_stats()
        print(f"\n{'='*50}")
        print(f"  PAPER TRADING STATS")
        print(f"{'='*50}")
        print(f"  Total trades : {s['total_trades']}  (open: {s['open_count']}, closed: {s['closed_count']})")
        if s["closed_count"]:
            print(f"  Win rate     : {s['win_rate']*100:.1f}%")
            print(f"  Total P&L    : ${s['total_pnl']:.2f}")
            print(f"  Avg P&L/trade: ${s['avg_pnl']:.2f}")
            print(f"  Sharpe       : {s['sharpe']}")
            print(f"  Profit factor: {s['profit_factor']}")
            print(f"\n  By type:")
            for tt, d in s["by_type"].items():
                print(f"    {tt:15s}  {d['count']:3d} trades  {d['win_rate']*100:5.1f}% win  avg ${d['avg_pnl']:+.2f}")

    else:
        print(f"Unknown command: {cmd}. Use: generate | close | stats")
