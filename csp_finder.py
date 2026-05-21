# -*- coding: utf-8 -*-
"""
Cash-Secured Put (CSP) Finder
==============================
Selling a naked put = collect premium, get assigned shares at a discount if stock falls.
This is the first step of the Wheel Strategy.

THE WHEEL:
  Step 1 -- Sell a cash-secured put. Collect premium.
            If stock stays above strike --> keep premium, repeat.
            If stock falls below strike --> you buy 100 shares at the strike price.
  Step 2 -- Now you own shares. Sell a covered call against them. Collect more premium.
            If stock stays below call strike --> keep premium, repeat.
            If stock rises above call strike --> shares get called away at a profit.
  Repeat forever -- collecting premium every 30-45 days.

NOTE: Requires cash equal to (strike x 100) as collateral.
      e.g. selling a $125 put on COP requires $12,500 in your account.
      This is the FRAMEWORK -- have it ready for when capital grows.

Usage:
  python csp_finder.py COP                  -- default $500 max premium target
  python csp_finder.py COP 15000            -- up to $15,000 collateral
  python csp_finder.py COP 15000 45         -- 45 DTE instead of 30
"""
import sys
sys.path.insert(0, ".")

import numpy as np
import pandas as pd
from datetime import date
from math import log, sqrt
from scipy.stats import norm

from data.fetcher import fetch_ohlcv, fetch_options_chain
from indicators.volatility import calculate_hv_rank


def bs_prob_otm(S, K, T, sigma):
    """Probability stock stays ABOVE strike K at expiry (put stays OTM = you keep premium)."""
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    d2 = (log(S / K) + (-0.5 * sigma**2) * T) / (sigma * sqrt(T))
    return float(norm.cdf(d2))


def find_csps(ticker: str, max_collateral: float, target_dte: int = 30) -> None:
    print(f"\nFetching {ticker} options...")
    df_price = fetch_ohlcv(ticker, "2024-01-01", str(date.today()))
    current  = float(df_price["Close"].iloc[-1])

    log_ret  = np.log(df_price["Close"] / df_price["Close"].shift(1)).dropna()
    iv       = float(log_ret.tail(30).std() * np.sqrt(252))

    hvr      = calculate_hv_rank(df_price)
    hv_rank  = hvr["hv_rank"]

    chain    = fetch_options_chain(ticker, target_dte)
    if not chain or chain["puts"].empty:
        print("  No options data available.")
        return

    puts   = chain["puts"].copy()
    expiry = chain["expiry"]
    dte    = chain["dte"]
    T      = dte / 365

    expected_move = iv * current * (T ** 0.5)

    print(f"\n{'='*76}")
    print(f"  CASH-SECURED PUTS  --  {ticker}  |  Price: ${current:.2f}  |  Exp: {expiry} ({dte} DTE)")
    print(f"  Max collateral: ${max_collateral:,.0f}  |  HVR: {hv_rank:.0f}/100  |  Expected move: +/-${expected_move:.2f}")
    print(f"{'='*76}")
    print(f"""
  HOW A CASH-SECURED PUT WORKS:
  You SELL a put and collect premium immediately.
  You hold ${'{strike x 100}'} in cash as collateral (in case you get assigned).

  OUTCOME 1 -- Stock stays above your strike (most likely if you pick wisely):
    Put expires worthless. You keep 100% of the premium. Repeat next month.

  OUTCOME 2 -- Stock falls below your strike (assignment):
    You buy 100 shares at the strike price.
    Your real cost = strike - premium collected (you got a discount).
    Then move to Step 2: sell a covered call against those shares.

  HVR {hv_rank:.0f}/100 -- {"GREAT time to sell puts, premium is elevated" if hv_rank >= 50 else "moderate premium -- still works"}
""")

    # Filter liquid puts below current price
    puts = puts[
        (puts["bid"] > 0) &
        (puts["ask"] > 0) &
        (puts["openInterest"] > 500) &
        (puts["strike"] < current) &
        (puts["strike"] > current * 0.75)
    ].reset_index(drop=True)

    results = []

    for _, row in puts.iterrows():
        strike     = row["strike"]
        bid        = row["bid"]
        collateral = strike * 100

        if collateral > max_collateral:
            continue

        premium_collected = bid * 100
        effective_cost    = strike - bid              # what you'd actually pay per share if assigned
        annualized_return = (bid / strike) * (365 / dte) * 100
        prob_keep         = bs_prob_otm(current, strike, T, iv)
        discount_pct      = (current - effective_cost) / current

        results.append({
            "strike":        strike,
            "bid":           bid,
            "premium":       premium_collected,
            "collateral":    collateral,
            "effective_cost":effective_cost,
            "discount":      discount_pct,
            "ann_return":    annualized_return,
            "prob_keep":     prob_keep,
            "breakeven":     effective_cost,
        })

    if not results:
        print(f"  No CSPs found under ${max_collateral:,.0f} collateral.")
        return

    # Sort by annualized return
    results.sort(key=lambda x: x["ann_return"], reverse=True)

    # Group: aggressive (closer to ATM, higher premium) vs conservative (farther OTM, safer)
    atm_pct  = current * 0.97
    otm_pct  = current * 0.90
    safe_pct = current * 0.85

    tiers = [
        (atm_pct,  current, "AGGRESSIVE  (2-5% OTM, high premium, higher assignment risk)"),
        (otm_pct,  atm_pct, "MODERATE    (5-10% OTM, balanced premium vs safety)"),
        (safe_pct, otm_pct, "CONSERVATIVE (10-15% OTM, lower premium, very safe)"),
    ]

    shown = 0
    for lo, hi, label in tiers:
        tier = [r for r in results if lo <= r["strike"] < hi]
        if not tier:
            continue
        best = sorted(tier, key=lambda x: x["ann_return"], reverse=True)[:2]
        print(f"  -- {label} --")
        for r in best:
            print(f"  Sell ${r['strike']:.0f} put  @ ${r['bid']:.2f}")
            print(f"    Premium collected : ${r['premium']:.0f}  (yours if stock stays above ${r['strike']:.0f})")
            print(f"    Collateral needed : ${r['collateral']:,.0f}")
            print(f"    If assigned       : buy 100 shares at ${r['effective_cost']:.2f}  "
                  f"(${current - r['effective_cost']:.2f} discount from current price, {r['discount']:.1%} off)")
            print(f"    Annualized return : {r['ann_return']:.1f}%  |  Prob of keeping premium: {r['prob_keep']:.1%}")
            print(f"    Entry cmd         : python positions.py add-option {ticker} "
                  f"{r['strike']:.0f} {expiry} put 1 {r['bid']:.2f}")
            print()
            shown += 1

    print(f"{'='*76}")
    print(f"  WHEEL STRATEGY NOTES:")
    print(f"  1. Sell the put. Collect premium. ({dte} days)")
    print(f"  2a. Stock stays above strike --> premium is profit. Sell another put.")
    print(f"  2b. Stock assigned --> run 'python covered_call.py {ticker}' for next step")
    print(f"  Target: sell puts 30-45 DTE, close at 50% profit, repeat every month")
    print(f"  Annual yield target: 20-40% on collateral through premium collection")
    print(f"{'='*76}\n")


if __name__ == "__main__":
    ticker         = sys.argv[1].upper() if len(sys.argv) > 1 else input("Ticker: ").strip().upper()
    max_collateral = float(sys.argv[2])  if len(sys.argv) > 2 else float(input("Max collateral ($): ") or 15000)
    target_dte     = int(sys.argv[3])    if len(sys.argv) > 3 else 30
    find_csps(ticker, max_collateral, target_dte)
