# -*- coding: utf-8 -*-
"""
Live Options Chain Viewer
==========================
Shows the real bid/ask/IV/volume/OI from the market for any ticker.

Usage:
  python options_chain.py AAPL           -- calls + puts, closest to 90 DTE
  python options_chain.py AAPL 60        -- closest to 60 DTE
  python options_chain.py AAPL 90 puts   -- puts only
  python options_chain.py AAPL 90 calls  -- calls only (default)
"""
import sys
sys.path.insert(0, ".")

from datetime import date
import pandas as pd
import numpy as np
from math import log, sqrt, exp
from scipy.stats import norm

from data.fetcher import fetch_ohlcv, fetch_options_chain


# ------------------------------------------------------------------ #
#  Black-Scholes delta estimate (for chains that don't include it)    #
# ------------------------------------------------------------------ #

def bs_delta(S, K, T, sigma, option_type="call"):
    if T <= 0 or sigma <= 0:
        return 1.0 if (option_type == "call" and S > K) else 0.0
    d1 = (log(S / K) + (0.5 * sigma**2) * T) / (sigma * sqrt(T))
    return norm.cdf(d1) if option_type == "call" else norm.cdf(d1) - 1


# ------------------------------------------------------------------ #
#  Display helpers                                                     #
# ------------------------------------------------------------------ #

def liquidity_tag(volume, open_interest, spread_pct) -> str:
    if open_interest > 2000 and spread_pct < 0.05:
        return "HIGH"
    if open_interest > 500 and spread_pct < 0.12:
        return "OK  "
    return "LOW "


def print_chain(df: pd.DataFrame, current_price: float, option_type: str,
                dte: int, recommend_strike: float) -> None:
    """Print a formatted options chain table."""

    # Filter to strikes within 25% of current price
    lo = current_price * 0.75
    hi = current_price * 1.25
    df = df[(df["strike"] >= lo) & (df["strike"] <= hi)].copy()

    if df.empty:
        print("  No strikes found in range.")
        return

    T = dte / 365

    print(f"\n  {'Strike':>8}  {'Bid':>7}  {'Ask':>7}  {'Mid':>7}  "
          f"{'Spread':>7}  {'IV':>6}  {'Delta':>6}  "
          f"{'Volume':>7}  {'OI':>7}  {'Liq':>5}  Note")
    print(f"  {'-'*100}")

    for _, row in df.iterrows():
        strike = row.get("strike", 0)
        bid    = row.get("bid",    0) or 0
        ask    = row.get("ask",    0) or 0
        iv_raw = row.get("impliedVolatility", 0) or 0
        vol    = int(row.get("volume",       0) or 0)
        oi     = int(row.get("openInterest", 0) or 0)
        itm    = bool(row.get("inTheMoney",  False))

        mid        = (bid + ask) / 2 if bid and ask else 0
        spread     = ask - bid if bid and ask else 0
        spread_pct = spread / mid if mid > 0 else 1.0
        iv         = iv_raw * 100  # convert to percentage

        delta = bs_delta(current_price, strike, T, iv_raw, option_type)

        liq  = liquidity_tag(vol, oi, spread_pct)
        itm_label = "ITM" if itm else "OTM"

        # Special markers
        note = ""
        if abs(strike - current_price) / current_price < 0.01:
            note = "<-- ATM"
        elif abs(strike - recommend_strike) / recommend_strike < 0.02:
            note = "<-- RECOMMENDED (5% OTM, 90d)"
        elif itm and abs(strike - current_price) / current_price < 0.03:
            note = "<-- near ATM"

        # Skip rows with no market data
        if bid == 0 and ask == 0 and vol == 0:
            continue

        # Separator at ATM
        if not itm and (df.index.get_loc(_) == 0 or
                        df.iloc[df.index.get_loc(_) - 1].get("inTheMoney", False)):
            print(f"  {'--- ATM ---':>8}  current price ${current_price:.2f}")

        print(
            f"  {strike:>8.2f}  "
            f"${bid:>6.2f}  "
            f"${ask:>6.2f}  "
            f"${mid:>6.2f}  "
            f"${spread:>5.2f}({spread_pct:>4.1%})  "
            f"{iv:>5.1f}%  "
            f"{delta:>6.3f}  "
            f"{vol:>7,}  "
            f"{oi:>7,}  "
            f"{liq}  "
            f"{itm_label}  {note}"
        )


# ------------------------------------------------------------------ #
#  Main                                                                #
# ------------------------------------------------------------------ #

def run(ticker: str, target_dte: int = 90, show: str = "both") -> None:
    print(f"\nFetching {ticker} options chain...")

    # Current stock price
    df_price = fetch_ohlcv(ticker, "2024-01-01", str(date.today()))
    current_price = float(df_price["Close"].iloc[-1])

    # Options chain
    chain = fetch_options_chain(ticker, target_dte)
    if not chain:
        print(f"  No options data available for {ticker}.")
        print("  Note: options data requires a valid equity ticker (not all ETFs/indices have chains).")
        return

    expiry  = chain["expiry"]
    dte     = chain["dte"]
    rec_strike = round(current_price * 1.05 / 1) * 1  # 5% OTM

    print(f"\n{'='*80}")
    print(f"  {ticker} OPTIONS CHAIN  --  {date.today()}")
    print(f"  Current Price: ${current_price:.2f}  |  Expiry: {expiry}  |  DTE: {dte}")
    print(f"  Recommended strike (5% OTM): ~${rec_strike:.0f}")
    print(f"{'='*80}")

    # All available expiries
    print(f"\n  ALL AVAILABLE EXPIRATIONS:")
    for exp, d in chain["all_expiries"]:
        marker = " <-- selected" if exp == expiry else ""
        dte_label = f"{d}d"
        print(f"    {exp}  ({dte_label}){marker}")

    # Calls
    if show in ("calls", "both"):
        print(f"\n  CALLS  (buy if you think {ticker} goes UP)")
        print_chain(chain["calls"], current_price, "call", dte, rec_strike)

    # Puts
    if show in ("puts", "both"):
        print(f"\n  PUTS  (buy if you think {ticker} goes DOWN)")
        print_chain(chain["puts"], current_price, "put", dte, current_price * 0.95)

    # Summary guidance
    print(f"\n{'='*80}")
    print(f"  HOW TO READ THIS:")
    print(f"  Bid/Ask  -- what buyers are offering / what sellers are asking")
    print(f"  Spread   -- the cost to enter. Tighter is better. Avoid > 10% spread.")
    print(f"  IV       -- implied volatility. Higher = more expensive option.")
    print(f"  Delta    -- how much the option moves per $1 move in {ticker}")
    print(f"             0.50 = ATM  |  0.25-0.40 = slight OTM (good for long calls)")
    print(f"  Volume   -- contracts traded today. Higher = easier to exit.")
    print(f"  OI       -- open interest. Higher = more liquid market.")
    print(f"  Liq      -- HIGH/OK/LOW liquidity based on OI + spread")
    print(f"\n  TO ADD THIS POSITION:")
    print(f"  python positions.py add-option {ticker} <STRIKE> {expiry} call <CONTRACTS> <PREMIUM>")
    print(f"  Example: python positions.py add-option {ticker} {rec_strike:.0f} {expiry} call 1 <ASK>")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        ticker = input("Enter ticker: ").strip().upper()
        target_dte = 90
        show = "both"
    else:
        ticker     = sys.argv[1].upper()
        target_dte = int(sys.argv[2])   if len(sys.argv) > 2 and sys.argv[2].isdigit() else 90
        show       = sys.argv[3].lower() if len(sys.argv) > 3 else "both"

    run(ticker, target_dte, show)
