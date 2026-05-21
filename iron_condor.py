# -*- coding: utf-8 -*-
"""
Iron Condor Finder
===================
An iron condor = Bull Put Spread + Bear Call Spread simultaneously.
You collect premium from BOTH sides and profit if the stock stays
in a range between your two short strikes by expiry.

Best used on:
  - Low volatility stocks that grind in a range (KO, WMT)
  - After a big move when the stock has settled
  - When HVR > 50 (options expensive -- great time to sell premium on both sides)

NOTE: You need Level 3 options approval on Robinhood for iron condors.
You do NOT need the capital yet -- this builds the framework for when you do.

Usage:
  python iron_condor.py KO              -- budget $500, 90 DTE
  python iron_condor.py KO 500          -- budget $500
  python iron_condor.py KO 1000 90      -- budget $1000, 90 DTE
"""
import sys
sys.path.insert(0, ".")

import numpy as np
import pandas as pd
from datetime import date
from math import log, sqrt
from scipy.stats import norm

from data.fetcher import fetch_ohlcv, fetch_options_chain
from indicators.volatility import calculate_hv_rank, spread_recommendation


def bs_prob_itm(S, K, T, sigma):
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    d2 = (log(S / K) + (-0.5 * sigma**2) * T) / (sigma * sqrt(T))
    return float(norm.cdf(d2))


def find_condors(ticker: str, budget: float, target_dte: int = 90) -> None:
    print(f"\nFetching {ticker} chain...")
    df_price = fetch_ohlcv(ticker, "2024-01-01", str(date.today()))
    current  = float(df_price["Close"].iloc[-1])

    log_ret  = np.log(df_price["Close"] / df_price["Close"].shift(1)).dropna()
    iv       = float(log_ret.tail(30).std() * np.sqrt(252))

    hvr      = calculate_hv_rank(df_price)
    hv_rank  = hvr["hv_rank"]

    chain    = fetch_options_chain(ticker, target_dte)
    if not chain or chain["calls"].empty or chain["puts"].empty:
        print("  No options data available.")
        return

    calls  = chain["calls"].copy()
    puts   = chain["puts"].copy()
    expiry = chain["expiry"]
    dte    = chain["dte"]
    T      = dte / 365

    expected_move = iv * current * (T ** 0.5)

    print(f"\n{'='*76}")
    print(f"  IRON CONDOR FINDER  --  {ticker}  |  Price: ${current:.2f}  |  Exp: {expiry} ({dte} DTE)")
    print(f"  Budget (max at risk per side): ${budget:.0f}")
    print(f"  HV Rank: {hv_rank:.0f}/100  |  Expected Move: +/- ${expected_move:.2f}  ({expected_move/current:.1%})")
    print(f"{'='*76}")

    print(f"""
  HOW AN IRON CONDOR WORKS:
  ----------------------------------------------------------------
  You sell a PUT spread below the stock  (collect premium)
  You sell a CALL spread above the stock (collect premium)
  Both expire worthless if the stock stays in the profit zone.

  Profit zone = between your two short strikes
  Max profit  = total credit from both spreads (collected day 1)
  Max loss    = spread width - total credit (on whichever side breaks)
  Break even  = short put - total credit  AND  short call + total credit

  Best stocks: slow movers with high HVR (expensive options)
  HVR {hv_rank:.0f}/100 -- {"IDEAL for iron condors" if hv_rank >= 50 else "moderate -- iron condors work but premium is thinner"}
  ----------------------------------------------------------------
""")

    # Filter liquid options
    liquid_calls = calls[(calls["bid"] > 0) & (calls["ask"] > 0) & (calls["openInterest"] > 500)].reset_index(drop=True)
    liquid_puts  = puts[ (puts["bid"]  > 0) & (puts["ask"]  > 0) & (puts["openInterest"]  > 500)].reset_index(drop=True)

    results = []

    # Find all valid bull put spreads (below current price)
    put_spreads = []
    for i, sell_row in liquid_puts.iterrows():
        sell_strike = sell_row["strike"]
        sell_bid    = sell_row["bid"]
        if sell_strike >= current or sell_strike < current * 0.80:
            continue
        for j, buy_row in liquid_puts.iterrows():
            if j >= i:
                continue
            buy_strike = buy_row["strike"]
            buy_ask    = buy_row["ask"]
            width = sell_strike - buy_strike
            if width < 5 or width > 20:
                continue
            credit = (sell_bid - buy_ask) * 100
            if credit <= 0:
                continue
            max_loss = (width * 100) - credit
            if max_loss > budget:
                continue
            put_spreads.append({
                "sell": sell_strike, "buy": buy_strike,
                "width": width, "credit": credit, "max_loss": max_loss,
                "breakeven": sell_strike - (sell_bid - buy_ask),
            })

    # Find all valid bear call spreads (above current price)
    call_spreads = []
    for i, sell_row in liquid_calls.iterrows():
        sell_strike = sell_row["strike"]
        sell_bid    = sell_row["bid"]
        if sell_strike <= current or sell_strike > current * 1.20:
            continue
        for j, buy_row in liquid_calls.iterrows():
            if j <= i:
                continue
            buy_strike = buy_row["strike"]
            buy_ask    = buy_row["ask"]
            width = buy_strike - sell_strike
            if width < 5 or width > 20:
                continue
            credit = (sell_bid - buy_ask) * 100
            if credit <= 0:
                continue
            max_loss = (width * 100) - credit
            if max_loss > budget:
                continue
            call_spreads.append({
                "sell": sell_strike, "buy": buy_strike,
                "width": width, "credit": credit, "max_loss": max_loss,
                "breakeven": sell_strike + (sell_bid - buy_ask),
            })

    # Combine into condors
    for ps in put_spreads:
        for cs in call_spreads:
            profit_zone_width = cs["sell"] - ps["sell"]
            if profit_zone_width <= 0:
                continue

            total_credit = ps["credit"] + cs["credit"]
            total_max_loss = max(ps["max_loss"], cs["max_loss"])  # max loss on either side
            be_lower = ps["breakeven"]
            be_upper = cs["breakeven"]

            # Probability both short strikes expire OTM
            prob_put_otm  = 1 - bs_prob_itm(current, ps["sell"], T, iv)
            prob_call_otm =     bs_prob_itm(current, cs["sell"], T, iv)
            prob_profit   = prob_put_otm * prob_call_otm  # rough joint probability

            rr = total_credit / total_max_loss

            results.append({
                "put_sell":    ps["sell"],
                "put_buy":     ps["buy"],
                "call_sell":   cs["sell"],
                "call_buy":    cs["buy"],
                "credit":      total_credit,
                "max_loss":    total_max_loss,
                "be_lower":    be_lower,
                "be_upper":    be_upper,
                "profit_zone": profit_zone_width,
                "prob_profit": prob_profit,
                "rr":          rr,
            })

    if not results:
        print(f"  No iron condors found under ${budget:.0f} per side.")
        print(f"  Try increasing budget or check if options are liquid enough.")
        return

    # Sort by probability of profit
    results.sort(key=lambda x: x["prob_profit"], reverse=True)
    top = results[:5]

    print(f"  TOP IRON CONDORS (sorted by probability of profit):\n")
    for i, r in enumerate(top, 1):
        print(f"  #{i}  Profit zone: ${r['put_sell']:.0f}  to  ${r['call_sell']:.0f}  "
              f"(${r['profit_zone']:.0f} wide)")
        print(f"       PUT  side: Sell ${r['put_sell']:.0f}  / Buy ${r['put_buy']:.0f} put")
        print(f"       CALL side: Sell ${r['call_sell']:.0f} / Buy ${r['call_buy']:.0f} call")
        print(f"       Total credit collected : ${r['credit']:.0f}  (yours to keep if stock stays in range)")
        print(f"       Max loss               : ${r['max_loss']:.0f}  (if stock breaks outside either spread)")
        print(f"       Break even lower       : ${r['be_lower']:.2f}  |  upper: ${r['be_upper']:.2f}")
        print(f"       Prob profit            : {r['prob_profit']:.1%}  |  R/R: {r['rr']:.2f}x")
        print(f"       Entry cmds:")
        print(f"         python positions.py add-option {ticker} {r['put_sell']:.0f} {expiry} put 1 {r['credit']/200:.2f}  (sell put leg)")
        print(f"         python positions.py add-option {ticker} {r['call_sell']:.0f} {expiry} call 1 {r['credit']/200:.2f}  (sell call leg)")
        print()

    print(f"{'='*76}")
    print(f"  NOTES:")
    print(f"  - Iron condors require Level 3 options approval on Robinhood")
    print(f"  - Close at 50% of max profit (don't hold to expiry)")
    print(f"  - Best when stock is range-bound, NOT when strongly trending")
    print(f"  - If stock breaks past a short strike, close the whole condor immediately")
    print(f"  - Expected move range: ${current - expected_move:.2f} to ${current + expected_move:.2f}")
    print(f"    Short strikes INSIDE this range = higher prob but more risk")
    print(f"    Short strikes OUTSIDE this range = lower prob but safer")
    print(f"{'='*76}\n")


if __name__ == "__main__":
    ticker     = sys.argv[1].upper() if len(sys.argv) > 1 else input("Ticker: ").strip().upper()
    budget     = float(sys.argv[2])  if len(sys.argv) > 2 else float(input("Max budget per side ($): ") or 500)
    target_dte = int(sys.argv[3])    if len(sys.argv) > 3 else 90
    find_condors(ticker, budget, target_dte)
