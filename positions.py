# -*- coding: utf-8 -*-
"""
Position Tracker — Stocks + Options
=====================================
Track open positions and get live hold/roll/sell recommendations.

STOCK COMMANDS:
  python positions.py add AAPL 298.97 10
      ticker, entry price, number of shares

OPTION COMMANDS:
  python positions.py add-option AAPL 313 2026-08-20 call 1 4.60
      ticker, strike, expiry (YYYY-MM-DD), call/put, contracts, premium paid

OTHER:
  python positions.py              -- show all positions
  python positions.py remove AAPL  -- remove by ticker (removes all legs for that ticker)
  python positions.py clear        -- remove everything
"""
import sys
import json
import os
from datetime import date, datetime
from math import log, sqrt, exp
from scipy.stats import norm
import numpy as np

sys.path.insert(0, ".")

from config import Config, DataConfig
from data.fetcher import fetch_ohlcv, fetch_vix, get_earnings_dates
from signals.generator import generate_signals
from indicators.volatility import calculate_atr, calculate_hv_rank

POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "positions.json")

SECTOR_MAP = {
    "COP":"Energy",   "EOG":"Energy",   "XOM":"Energy",   "CVX":"Energy",
    "AAPL":"Tech",    "MSFT":"Tech",    "NVDA":"Tech",    "AVGO":"Tech",    "AMD":"Tech",
    "WMT":"Staples",  "KO":"Staples",   "PG":"Staples",   "COST":"Staples",
    "FCX":"Materials","LIN":"Materials","NEM":"Materials",
    "CAT":"Industrials","DE":"Industrials",
    "GOOGL":"Comms",  "META":"Comms",
    "UNH":"Healthcare","JNJ":"Healthcare",
    "PLD":"RealEstate","AMT":"RealEstate",
    "JPM":"Financials","BAC":"Financials",
}

ROLL_WARNING_DAYS  = 35   # start warning to roll when DTE falls below this
ROLL_URGENT_DAYS   = 15   # urgent — roll or close immediately


# ------------------------------------------------------------------ #
#  Persistence                                                         #
# ------------------------------------------------------------------ #

def load_positions() -> list:
    if not os.path.exists(POSITIONS_FILE):
        return []
    with open(POSITIONS_FILE, "r") as f:
        return json.load(f)

def save_positions(positions: list) -> None:
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)

def add_stock(ticker: str, entry_price: float, shares: float) -> None:
    positions = load_positions()
    for p in positions:
        if p["kind"] == "stock" and p["ticker"] == ticker.upper():
            p.update({"entry_price": entry_price, "shares": shares,
                       "entry_date": str(date.today())})
            save_positions(positions)
            print(f"  Updated {ticker.upper()} stock position.")
            return
    positions.append({"kind": "stock", "ticker": ticker.upper(),
                       "entry_price": entry_price, "shares": shares,
                       "entry_date": str(date.today())})
    save_positions(positions)
    print(f"  Added {ticker.upper()} -- {shares:.0f} shares at ${entry_price:.2f}")

def add_option(ticker: str, strike: float, expiry: str, option_type: str,
               contracts: int, premium: float) -> None:
    """
    ticker      : underlying stock symbol  e.g. AAPL
    strike      : strike price             e.g. 313.00
    expiry      : expiration date          e.g. 2026-08-20
    option_type : 'call' or 'put'
    contracts   : number of contracts (1 contract = 100 shares)
    premium     : price paid per share     e.g. 4.60  (so 1 contract cost $460)
    """
    try:
        datetime.strptime(expiry, "%Y-%m-%d")
    except ValueError:
        print("  ERROR: expiry must be in YYYY-MM-DD format, e.g. 2026-08-20")
        return

    if option_type.lower() not in ("call", "put"):
        print("  ERROR: option_type must be 'call' or 'put'")
        return

    positions = load_positions()
    positions.append({
        "kind":        "option",
        "ticker":      ticker.upper(),
        "strike":      strike,
        "expiry":      expiry,
        "option_type": option_type.lower(),
        "contracts":   contracts,
        "premium":     premium,
        "entry_date":  str(date.today()),
    })
    save_positions(positions)
    cost = premium * contracts * 100
    print(f"  Added {ticker.upper()} {option_type.upper()} ${strike} exp {expiry}")
    print(f"  {contracts} contract(s) at ${premium:.2f}/share = ${cost:.2f} total cost")

def remove_position(ticker: str) -> None:
    positions = load_positions()
    before = len(positions)
    positions = [p for p in positions if p["ticker"] != ticker.upper()]
    if len(positions) < before:
        save_positions(positions)
        print(f"  Removed {ticker.upper()}.")
    else:
        print(f"  {ticker.upper()} not found.")


# ------------------------------------------------------------------ #
#  Options math                                                        #
# ------------------------------------------------------------------ #

def get_real_iv(ticker: str, strike: float, expiry: str,
                opt_type: str, current_price: float) -> float | None:
    """
    Look up real implied volatility from the live options chain.
    Uses py_vollib to solve for IV from the actual market bid/ask mid-price.
    Falls back to None if the options chain is unavailable.

    Returns IV as a float (e.g. 0.31 = 31%) or None on failure.
    """
    try:
        import yfinance as yf
        from py_vollib.black_scholes.implied_volatility import implied_volatility as bsiv

        ticker_obj = yf.Ticker(ticker)

        # yfinance requires the exact expiry string from the available dates
        available = ticker_obj.options
        if not available:
            return None

        # Find the closest available expiry to what we stored
        from datetime import datetime as _dt
        target_dt = _dt.strptime(expiry, "%Y-%m-%d")
        best_exp  = min(
            available,
            key=lambda d: abs((_dt.strptime(d, "%Y-%m-%d") - target_dt).days)
        )

        chain = ticker_obj.option_chain(best_exp)
        opts  = chain.calls if opt_type.lower() == "call" else chain.puts
        if opts.empty:
            return None

        # Find the row closest to our strike
        row = opts.iloc[(opts["strike"] - strike).abs().argsort()[:1]]
        if row.empty:
            return None

        bid = float(row["bid"].iloc[0] or 0)
        ask = float(row["ask"].iloc[0] or 0)
        if bid <= 0 or ask <= 0:
            return None
        mid = (bid + ask) / 2.0

        T_days = (_dt.strptime(best_exp, "%Y-%m-%d").date() -
                  _dt.today().date()).days
        T = max(T_days / 365.0, 1 / 365.0)

        flag = "c" if opt_type.lower() == "call" else "p"
        iv   = float(bsiv(mid, current_price, strike, T, 0.05, flag))

        # Sanity check: realistic IV range 1%–300%
        return iv if 0.01 < iv < 3.0 else None

    except Exception:
        return None   # silently fall back to historical vol


def bs_price(S, K, T, r, sigma, option_type):
    if T <= 0:
        return max(S - K, 0) if option_type == "call" else max(K - S, 0)
    d1 = (log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    if option_type == "call":
        return S * norm.cdf(d1) - K * exp(-r * T) * norm.cdf(d2)
    return K * exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

def bs_greeks(S, K, T, r, sigma):
    if T <= 0:
        return {"delta": 0.0, "theta": 0.0, "vega": 0.0}
    d1 = (log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    delta = norm.cdf(d1)
    theta = (-(S * norm.pdf(d1) * sigma) / (2 * sqrt(T))
             - 0.05 * K * exp(-0.05 * T) * norm.cdf(d2)) / 365
    vega  = S * norm.pdf(d1) * sqrt(T) / 100
    return {"delta": delta, "theta": theta, "vega": vega}

def estimate_iv(df) -> float:
    """Historical 30-day vol as IV proxy — no live options chain needed."""
    log_ret = np.log(df["Close"] / df["Close"].shift(1)).dropna()
    return float(log_ret.tail(30).std() * np.sqrt(252))


# ------------------------------------------------------------------ #
#  Recommendation logic                                                #
# ------------------------------------------------------------------ #

def stock_recommendation(entry_price, current_price, regime, mtf_score,
                          entry_signal, stop_loss_pct=0.08):
    if current_price <= entry_price * (1 - stop_loss_pct):
        return "SELL  -- Stop loss hit", "SELL"
    if regime == 0:
        return "SELL  -- Death cross, trend reversed", "SELL"
    if mtf_score == 3 and entry_signal == 1 and (current_price - entry_price) / entry_price < 0.03:
        return "BUY MORE -- 3/3 signal, good add point", "BUY"
    if regime == 1 and mtf_score >= 2:
        return "HOLD  -- Trend intact", "HOLD"
    return "HOLD  -- Monitoring", "HOLD"

def option_recommendation(dte, regime, mtf_score, pnl_pct, option_kind="debit"):
    if dte <= 0:
        return "CLOSE -- Option has expired", "CLOSE"
    if regime == 0:
        return "CLOSE -- Death cross on underlying, exit immediately", "CLOSE"
    if dte <= ROLL_URGENT_DAYS:
        return f"ROLL NOW -- Only {dte} DTE left, close and buy new 90-day contract", "ROLL"
    if dte <= ROLL_WARNING_DAYS:
        return f"ROLL SOON -- {dte} DTE, start planning roll to new 90-day contract", "ROLL"
    # 50% profit target rule — standard options management
    # Debit spread: take profit when value has grown 50%+ over cost
    # Credit spread: take profit when you've kept 50%+ of the premium (pnl is 50% of max)
    if option_kind == "debit" and pnl_pct >= 0.50:
        return "TAKE PROFIT -- 50%+ gain reached, close and redeploy capital", "CLOSE"
    if option_kind == "credit" and pnl_pct >= 0.50:
        return "TAKE PROFIT -- Collected 50%+ of max credit, close and redeploy", "CLOSE"
    if mtf_score == 3 and regime == 1:
        return "HOLD  -- Trend strong, plenty of time remaining", "HOLD"
    if mtf_score >= 2 and regime == 1:
        return "HOLD  -- Trend intact", "HOLD"
    return "HOLD  -- Monitoring", "HOLD"


# ------------------------------------------------------------------ #
#  Display                                                             #
# ------------------------------------------------------------------ #

def show_positions() -> None:
    positions = load_positions()
    if not positions:
        print("\n  No positions tracked yet.")
        print("\n  Add a stock:  python positions.py add AAPL 298.97 10")
        print("  Add option:   python positions.py add-option AAPL 313 2026-08-20 call 1 4.60")
        return

    print(f"\n{'='*78}")
    print(f"  POSITION TRACKER  --  {date.today()}")
    print(f"{'='*78}")

    vix = fetch_vix("2020-01-01", str(date.today()))
    total_cost = 0.0
    total_value = 0.0

    for p in positions:
        # backward compatibility — positions saved before "kind" field was added
        if "kind" not in p:
            p["kind"] = "stock"
        ticker = p["ticker"]
        try:
            cfg = Config(data=DataConfig(ticker=ticker))
            df  = fetch_ohlcv(ticker, "2020-01-01", str(date.today()))
            earnings = get_earnings_dates(ticker)
            df  = generate_signals(df, cfg.indicators, vix=vix, earnings_dates=earnings)

            last          = df.iloc[-1]
            current_price = float(last["Close"])
            regime        = int(last.get("Regime", 0))
            mtf_score     = int(last.get("MTF_Score", 0))
            entry_signal  = int(last.get("Entry_Signal", 0))
            w_regime      = int(last.get("W_Regime", 0))
            m_regime      = int(last.get("M_Regime", 0))
            iv            = estimate_iv(df)

            d_label = "BULL" if regime   == 1 else "BEAR"
            w_label = "BULL" if w_regime == 1 else "BEAR"
            m_label = "BULL" if m_regime == 1 else "BEAR"

            # ── STOCK position ─────────────────────────────────────
            if p["kind"] == "stock":
                entry_price = p["entry_price"]
                shares      = p["shares"]
                days_held   = (date.today() - datetime.strptime(p["entry_date"], "%Y-%m-%d").date()).days
                pnl_dollar  = (current_price - entry_price) * shares
                pnl_pct     = (current_price - entry_price) / entry_price
                atr_series  = calculate_atr(df)
                atr_val     = float(atr_series.iloc[-1])
                stop_price  = entry_price - (2 * atr_val)   # 2x ATR stop
                stop_dist   = (current_price - stop_price) / current_price
                cost        = entry_price * shares
                value       = current_price * shares

                rec, status = stock_recommendation(
                    entry_price, current_price, regime, mtf_score, entry_signal)
                tag = {"SELL": "[ SELL ]", "BUY": "[BUY MORE]"}.get(status, "[  HOLD  ]")

                hvr         = calculate_hv_rank(df)
                sign = "+" if pnl_dollar >= 0 else ""
                print(f"\n  {ticker}  [STOCK]")
                print(f"  {'Entry':>10}: ${entry_price:.2f} x{shares:.0f} shares  ({p['entry_date']}, {days_held}d ago)")
                print(f"  {'Now':>10}: ${current_price:.2f}   P&L: {sign}${pnl_dollar:.2f} ({sign}{pnl_pct:.2%})")
                print(f"  {'ATR stop':>10}: ${stop_price:.2f}   ({stop_dist:.1%} away)  [ATR = ${atr_val:.2f}]")
                print(f"  {'Signals':>10}: Monthly {m_label} | Weekly {w_label} | Daily {d_label} | Score {mtf_score}/3")
                print(f"  {'Vol/IV':>10}: HV Rank {hvr['hv_rank']:.0f}/100  |  Current HV {hvr['current_hv']:.1%}")
                print(f"  {tag} {rec}")
                total_cost  += cost
                total_value += value

            # ── OPTION position ────────────────────────────────────
            elif p["kind"] == "option":
                strike      = p["strike"]
                expiry_dt   = datetime.strptime(p["expiry"], "%Y-%m-%d").date()
                dte         = (expiry_dt - date.today()).days
                option_type = p["option_type"]
                contracts   = p["contracts"]
                premium     = p["premium"]
                days_held   = (date.today() - datetime.strptime(p["entry_date"], "%Y-%m-%d").date()).days

                T_now        = max(dte / 365, 0)
                current_val  = bs_price(current_price, strike, T_now, 0.05, iv, option_type)
                greeks       = bs_greeks(current_price, strike, T_now, 0.05, iv)

                cost_total   = premium * contracts * 100
                value_total  = current_val * contracts * 100
                pnl_dollar   = value_total - cost_total
                pnl_pct      = pnl_dollar / cost_total if cost_total > 0 else 0
                sign         = "+" if pnl_dollar >= 0 else ""

                moneyness = "ITM" if (option_type == "call" and current_price > strike) or \
                                     (option_type == "put"  and current_price < strike) else "OTM"

                rec, status = option_recommendation(dte, regime, mtf_score, pnl_pct)
                tag = {"CLOSE": "[ CLOSE ]", "ROLL": "[ ROLL  ]"}.get(status, "[  HOLD  ]")

                print(f"\n  {ticker}  [{option_type.upper()} OPTION]")
                print(f"  {'Contract':>10}: ${strike:.0f} strike  |  Expires {p['expiry']}  |  {dte} DTE  |  {moneyness}")
                print(f"  {'Size':>10}: {contracts} contract(s) = {contracts*100} shares notional")
                print(f"  {'Entry':>10}: ${premium:.2f}/share  =  ${cost_total:.2f} total paid  ({p['entry_date']}, {days_held}d ago)")
                print(f"  {'Now':>10}: ${current_val:.2f}/share  =  ${value_total:.2f} total value")
                print(f"  {'P&L':>10}: {sign}${pnl_dollar:.2f}  ({sign}{pnl_pct:.2%})")
                print(f"  {'Greeks':>10}: Delta {greeks['delta']:.3f}  |  Theta {greeks['theta']:.4f}/day  |  Vega {greeks['vega']:.4f}/1%vol")
                hvr = calculate_hv_rank(df)
                print(f"  {'Stock':>10}: ${current_price:.2f}  |  IV est. {iv:.1%}  |  HV Rank {hvr['hv_rank']:.0f}/100  |  Signals {mtf_score}/3  ({m_label}/{w_label}/{d_label})")
                print(f"  {tag} {rec}")

                # Roll target — show what a new 90-day contract would look like
                if status == "ROLL":
                    new_T   = 90 / 365
                    new_K   = current_price * 1.05
                    new_val = bs_price(current_price, new_K, new_T, 0.05, iv, option_type)
                    new_exp = pd.bdate_range(start=date.today(), periods=66)[-1].date() \
                              if True else "~90 days out"
                    print(f"  {'Roll to':>10}: New 90-day {option_type} at ${new_K:.0f} strike  ~${new_val:.2f}/share premium")

                total_cost  += cost_total
                total_value += value_total

        except Exception as e:
            print(f"\n  {ticker}  --  ERROR: {e}")

        print(f"  {'-'*70}")

    # Portfolio summary
    total_pnl     = total_value - total_cost
    total_pnl_pct = total_pnl / total_cost if total_cost > 0 else 0
    sign = "+" if total_pnl >= 0 else ""
    print(f"\n  PORTFOLIO TOTAL")
    print(f"  Cost basis : ${total_cost:,.2f}")
    print(f"  Value now  : ${total_value:,.2f}")
    print(f"  Total P&L  : {sign}${total_pnl:,.2f}  ({sign}{total_pnl_pct:.2%})")

    vix_val = float(vix.iloc[-1]) if not vix.empty else 0
    print(f"\n  VIX: {vix_val:.1f}  --  ", end="")
    if vix_val < 20:
        print("Market calm.")
    elif vix_val < 30:
        print("Market elevated -- consider reducing exposure.")
    else:
        print("Market panic -- protect positions.")

    # Portfolio heat check — sector concentration warning
    sector_counts = {}
    for p in positions:
        if "kind" not in p:
            p["kind"] = "stock"
        sector = SECTOR_MAP.get(p["ticker"], "Other")
        sector_counts[sector] = sector_counts.get(sector, 0) + 1

    if sector_counts:
        print(f"\n  PORTFOLIO HEAT CHECK:")
        for sector, count in sorted(sector_counts.items(), key=lambda x: -x[1]):
            bar    = "#" * count
            warn   = "  [!] CONCENTRATED -- consider reducing" if count >= 3 else \
                     "  [~] watch correlation" if count == 2 else ""
            print(f"  {sector:<14} {bar:<6} {count} position(s){warn}")
        if any(c >= 3 for c in sector_counts.values()):
            print(f"\n  WARNING: 3+ positions in one sector move together.")
            print(f"  A single sector event wipes all of them at once.")

    print(f"{'='*78}\n")


# ------------------------------------------------------------------ #
#  Spread positions                                                    #
# ------------------------------------------------------------------ #

def add_spread(ticker: str, long_strike: float, short_strike: float,
               option_type: str, contracts: int, net_cost: float, expiry: str) -> None:
    """
    Add a two-leg spread position.

    long_strike  : strike you BOUGHT  (lower for bull call / put protection leg)
    short_strike : strike you SOLD
    option_type  : 'call' or 'put'
    net_cost     : positive = debit paid  (e.g. 4.27)
                   negative = credit received  (e.g. -3.00)
    expiry       : YYYY-MM-DD
    """
    try:
        datetime.strptime(expiry, "%Y-%m-%d")
    except ValueError:
        print("  ERROR: expiry must be YYYY-MM-DD")
        return
    if option_type.lower() not in ("call", "put"):
        print("  ERROR: option_type must be 'call' or 'put'")
        return
    positions = load_positions()
    positions.append({
        "kind":         "spread",
        "ticker":       ticker.upper(),
        "long_strike":  long_strike,
        "short_strike": short_strike,
        "option_type":  option_type.lower(),
        "contracts":    contracts,
        "net_cost":     net_cost,   # + = debit, - = credit
        "expiry":       expiry,
        "entry_date":   str(date.today()),
    })
    save_positions(positions)
    cost_total  = abs(net_cost) * contracts * 100
    direction   = "Debit — paid" if net_cost > 0 else "Credit — collected"
    print(f"  Added {ticker.upper()} ${long_strike:.0f}/{short_strike:.0f} {option_type.upper()} spread")
    print(f"  {direction}: ${abs(net_cost):.2f}/share  =  ${cost_total:.2f} total  |  Expires {expiry}")


def remove_position_by_id(idx: int) -> bool:
    """Remove a position by its list index. Returns True if removed."""
    positions = load_positions()
    if 0 <= idx < len(positions):
        positions.pop(idx)
        save_positions(positions)
        return True
    return False


# ------------------------------------------------------------------ #
#  Web data API (structured, no printing)                              #
# ------------------------------------------------------------------ #

def _fetch_ticker_web(ticker: str, vix) -> dict:
    """Fetch live price + signal data for one ticker. Used by get_positions_web_data."""
    import yfinance as yf
    live_price = None
    try:
        fi         = yf.Ticker(ticker).fast_info
        live_price = float(fi.get("lastPrice") or 0) or None
    except Exception:
        pass
    try:
        cfg      = Config(data=DataConfig(ticker=ticker))
        df       = fetch_ohlcv(ticker, "2020-01-01", str(date.today()))
        earnings = get_earnings_dates(ticker)
        df       = generate_signals(df, cfg.indicators, vix=vix, earnings_dates=earnings)
        last     = df.iloc[-1]
        iv       = estimate_iv(df)
        current  = live_price if live_price else float(last["Close"])
        return {
            "current_price":  current,
            "regime":         int(last.get("Regime",      0)),
            "mtf_score":      int(last.get("MTF_Score",   0)),
            "w_regime":       int(last.get("W_Regime",    0)),
            "m_regime":       int(last.get("M_Regime",    0)),
            "entry_signal":   int(last.get("Entry_Signal",0)),
            "iv":             iv,
            "error":          None,
        }
    except Exception as e:
        return {"current_price": live_price or 0, "error": str(e)}


def get_positions_web_data() -> dict:
    """
    Return structured P&L data for all positions — used by the /positions web route.
    Fetches live prices in parallel (one request per unique ticker).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    positions = load_positions()
    if not positions:
        return {"positions": [], "total_cost": 0, "total_value": 0,
                "total_pnl": 0, "total_pnl_pct": 0, "vix": 0, "vix_note": ""}

    # VIX — one fetch, shared
    try:
        vix     = fetch_vix("2020-01-01", str(date.today()))
        vix_val = float(vix.iloc[-1]) if not vix.empty else 0
    except Exception:
        vix, vix_val = None, 0

    vix_note = ("Market calm" if vix_val < 20
                else "Elevated — consider reducing exposure" if vix_val < 30
                else "Panic — protect positions")

    # Parallel ticker fetch (deduplicated)
    unique_tickers = list({p.get("ticker", "") for p in positions if p.get("ticker")})
    ticker_data    = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_ticker_web, t, vix): t for t in unique_tickers}
        for fut in as_completed(futures):
            ticker_data[futures[fut]] = fut.result()

    result      = []
    total_cost  = 0.0
    total_value = 0.0

    for idx, p in enumerate(positions):
        if "kind" not in p:
            p["kind"] = "stock"

        ticker = p.get("ticker", "?")
        td     = ticker_data.get(ticker, {})

        try:
            days_held = (date.today() -
                         datetime.strptime(p.get("entry_date", str(date.today())), "%Y-%m-%d").date()).days
        except Exception:
            days_held = 0

        pos = {
            "idx":        idx,
            "ticker":     ticker,
            "kind":       p["kind"],
            "entry_date": p.get("entry_date", ""),
            "days_held":  days_held,
            "error":      td.get("error"),
        }

        if td.get("error") or not td:
            result.append(pos)
            continue

        current_price = td.get("current_price", 0)
        regime        = td.get("regime",    0)
        mtf_score     = td.get("mtf_score", 0)
        iv            = td.get("iv", 0.20)

        pos.update({
            "current_price": current_price,
            "regime":        regime,
            "mtf_score":     mtf_score,
            "daily":   "BULL" if regime == 1              else "BEAR",
            "weekly":  "BULL" if td.get("w_regime", 0) == 1 else "BEAR",
            "monthly": "BULL" if td.get("m_regime", 0) == 1 else "BEAR",
            "iv":      iv,
        })

        try:
            # ── STOCK ──────────────────────────────────────────────
            if p["kind"] == "stock":
                entry = p["entry_price"]
                shares = p["shares"]
                cost   = entry * shares
                value  = current_price * shares
                pnl    = value - cost
                pnl_pct = pnl / cost if cost else 0
                rec, status = stock_recommendation(
                    entry, current_price, regime, mtf_score, td.get("entry_signal", 0))
                pos.update({
                    "entry_price": entry, "shares": shares,
                    "cost": cost, "value": value, "pnl": pnl, "pnl_pct": pnl_pct,
                    "recommendation": rec, "status": status,
                })
                total_cost  += cost
                total_value += value

            # ── SINGLE OPTION ──────────────────────────────────────
            elif p["kind"] == "option":
                strike      = p["strike"]
                expiry_dt   = datetime.strptime(p["expiry"], "%Y-%m-%d").date()
                dte         = (expiry_dt - date.today()).days
                T           = max(dte / 365, 0)
                opt_type    = p["option_type"]
                contracts   = p["contracts"]
                premium     = p["premium"]
                # Try real IV from live options chain; fall back to historical vol
                real_iv = get_real_iv(ticker, strike, p["expiry"], opt_type, current_price)
                iv_used = real_iv if real_iv else iv
                cur_val     = bs_price(current_price, strike, T, 0.05, iv_used, opt_type)
                greeks      = bs_greeks(current_price, strike, T, 0.05, iv)
                cost        = premium * contracts * 100
                value       = cur_val  * contracts * 100
                pnl         = value - cost
                pnl_pct     = pnl / cost if cost else 0
                moneyness   = ("ITM" if (opt_type == "call" and current_price > strike) or
                                        (opt_type == "put"  and current_price < strike) else "OTM")
                rec, status = option_recommendation(dte, regime, mtf_score, pnl_pct)
                pos.update({
                    "strike": strike, "expiry": p["expiry"], "dte": dte,
                    "option_type": opt_type, "contracts": contracts,
                    "premium": premium, "current_val": cur_val,
                    "cost": cost, "value": value, "pnl": pnl, "pnl_pct": pnl_pct,
                    "moneyness": moneyness,
                    "delta": greeks["delta"], "theta": greeks["theta"], "vega": greeks["vega"],
                    "iv_real": real_iv,   # None = fell back to historical vol
                    "recommendation": rec, "status": status,
                })
                total_cost  += cost
                total_value += value

            # ── SPREAD ─────────────────────────────────────────────
            elif p["kind"] == "spread":
                ls        = p["long_strike"]
                ss        = p["short_strike"]
                opt_type  = p["option_type"]
                net_cost  = p["net_cost"]     # + debit, - credit
                contracts = p["contracts"]
                expiry_dt = datetime.strptime(p["expiry"], "%Y-%m-%d").date()
                dte       = (expiry_dt - date.today()).days
                T         = max(dte / 365, 0)

                # Try to get real IV from the long leg's live market price
                real_iv  = get_real_iv(ticker, ls, p["expiry"], opt_type, current_price)
                iv_used  = real_iv if real_iv else iv
                long_val  = bs_price(current_price, ls, T, 0.05, iv_used, opt_type)
                short_val = bs_price(current_price, ss, T, 0.05, iv_used, opt_type)

                is_debit  = net_cost > 0
                if is_debit:
                    cur_spread  = long_val - short_val
                    pnl_per     = cur_spread - net_cost
                else:
                    cur_spread  = short_val - long_val   # cost to close
                    pnl_per     = abs(net_cost) - cur_spread

                width       = abs(ss - ls)
                cost        = abs(net_cost) * contracts * 100
                value       = max(cur_spread, 0) * contracts * 100
                pnl         = pnl_per * contracts * 100
                pnl_pct     = pnl_per / abs(net_cost) if net_cost else 0

                if is_debit:
                    max_profit  = (width - abs(net_cost)) * contracts * 100
                    max_loss    = abs(net_cost) * contracts * 100
                    breakeven   = ls + abs(net_cost)
                    type_label  = f"Bull {'Call' if opt_type == 'call' else 'Put'} Spread (Debit)"
                else:
                    max_profit  = abs(net_cost) * contracts * 100
                    max_loss    = (width - abs(net_cost)) * contracts * 100
                    breakeven   = ss - abs(net_cost)
                    type_label  = f"Bull {'Put' if opt_type == 'put' else 'Call'} Spread (Credit)"

                rec, status = option_recommendation(
                    dte, regime, mtf_score, pnl_pct, "debit" if is_debit else "credit")
                pos.update({
                    "long_strike": ls, "short_strike": ss,
                    "option_type": opt_type, "net_cost": net_cost,
                    "contracts": contracts, "expiry": p["expiry"], "dte": dte,
                    "cur_spread": cur_spread, "cost": cost, "value": value,
                    "pnl": pnl, "pnl_pct": pnl_pct,
                    "max_profit": max_profit, "max_loss": max_loss, "breakeven": breakeven,
                    "type_label": type_label, "is_debit": is_debit,
                    "iv_used": round(iv_used, 4),   # shows whether real or historical IV was used
                    "iv_real": real_iv,
                    "recommendation": rec, "status": status,
                })
                total_cost  += cost
                total_value += value

        except Exception as e:
            pos["error"] = str(e)

        result.append(pos)

    total_pnl = total_value - total_cost
    return {
        "positions":     result,
        "total_cost":    round(total_cost,  2),
        "total_value":   round(total_value, 2),
        "total_pnl":     round(total_pnl,   2),
        "total_pnl_pct": total_pnl / total_cost if total_cost else 0,
        "vix":           round(vix_val, 1),
        "vix_note":      vix_note,
    }


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    import pandas as pd
    args = sys.argv[1:]

    if not args:
        show_positions()

    elif args[0] == "add":
        if len(args) < 4:
            print("Usage:   python positions.py add TICKER ENTRY_PRICE SHARES")
            print("Example: python positions.py add AAPL 298.97 10")
        else:
            add_stock(args[1], float(args[2]), float(args[3]))
            show_positions()

    elif args[0] == "add-option":
        if len(args) < 7:
            print("Usage:   python positions.py add-option TICKER STRIKE EXPIRY TYPE CONTRACTS PREMIUM")
            print("Example: python positions.py add-option AAPL 313 2026-08-20 call 1 4.60")
        else:
            add_option(args[1], float(args[2]), args[3], args[4], int(args[5]), float(args[6]))
            show_positions()

    elif args[0] == "remove":
        if len(args) < 2:
            print("Usage: python positions.py remove TICKER")
        else:
            remove_position(args[1])

    elif args[0] == "add-spread":
        # add-spread AAPL 310 330 call 1 4.27 2026-06-26
        if len(args) < 8:
            print("Usage:   python positions.py add-spread TICKER LONG_STRIKE SHORT_STRIKE TYPE CONTRACTS NET_COST EXPIRY")
            print("Example: python positions.py add-spread AAPL 310 330 call 1 4.27 2026-06-26")
            print("         (use negative NET_COST for a credit spread, e.g. -3.00)")
        else:
            add_spread(args[1], float(args[2]), float(args[3]), args[4],
                       int(args[5]), float(args[6]), args[7])
            show_positions()

    elif args[0] == "remove-id":
        if len(args) < 2:
            print("Usage: python positions.py remove-id INDEX")
        else:
            ok = remove_position_by_id(int(args[1]))
            print("  Removed." if ok else "  Index not found.")

    elif args[0] == "clear":
        save_positions([])
        print("  All positions cleared.")

    else:
        print("Unknown command.")
        print("  python positions.py                                                       -- view all")
        print("  python positions.py add AAPL 298.97 10                                   -- add stock")
        print("  python positions.py add-option AAPL 313 2026-08-20 call 1 4.60           -- add option")
        print("  python positions.py add-spread AAPL 310 330 call 1 4.27 2026-06-26       -- add spread")
        print("  python positions.py remove AAPL                                           -- remove by ticker")
        print("  python positions.py remove-id 0                                           -- remove by index")
        print("  python positions.py clear                                                  -- remove all")
