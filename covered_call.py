# -*- coding: utf-8 -*-
"""
Covered Call Analyzer
======================
If you own the stock, you can sell a call against it every month and collect premium.
This is Step 2 of the Wheel Strategy, and also a standalone income strategy.

You own 100 shares + sell 1 call = covered call.
Premium collected lowers your cost basis every month.

Usage:
  python covered_call.py COP                          -- if you own 100 shares
  python covered_call.py COP 125.11 100               -- entry price, shares
  python covered_call.py COP 125.11 100 45            -- 45 DTE
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


def bs_prob_otm_call(S, K, T, sigma):
    """Probability stock stays BELOW strike K (call stays OTM = you keep shares + premium)."""
    if T <= 0 or sigma <= 0:
        return 1.0 if S < K else 0.0
    d2 = (log(S / K) + (-0.5 * sigma**2) * T) / (sigma * sqrt(T))
    return float(1 - norm.cdf(d2))


def analyze_covered_calls(ticker: str, entry_price: float, shares: int, target_dte: int = 30) -> None:
    print(f"\nFetching {ticker} options...")
    df_price = fetch_ohlcv(ticker, "2024-01-01", str(date.today()))
    current  = float(df_price["Close"].iloc[-1])

    log_ret  = np.log(df_price["Close"] / df_price["Close"].shift(1)).dropna()
    iv       = float(log_ret.tail(30).std() * np.sqrt(252))

    hvr      = calculate_hv_rank(df_price)
    hv_rank  = hvr["hv_rank"]

    chain    = fetch_options_chain(ticker, target_dte)
    if not chain or chain["calls"].empty:
        print("  No options data available.")
        return

    calls  = chain["calls"].copy()
    expiry = chain["expiry"]
    dte    = chain["dte"]
    T      = dte / 365

    contracts = shares // 100
    if contracts == 0:
        print(f"  Need at least 100 shares to sell covered calls. You have {shares}.")
        return

    current_value = current * shares
    unrealized_pnl = (current - entry_price) * shares
    cost_basis = entry_price

    print(f"\n{'='*76}")
    print(f"  COVERED CALL ANALYZER  --  {ticker}  |  Price: ${current:.2f}  |  Exp: {expiry} ({dte} DTE)")
    print(f"  Your position: {shares} shares at ${entry_price:.2f}  |  Current value: ${current_value:,.2f}")
    pnl_sign = "+" if unrealized_pnl >= 0 else ""
    print(f"  Unrealized P&L: {pnl_sign}${unrealized_pnl:.2f}  |  HVR: {hv_rank:.0f}/100")
    print(f"  Selling {contracts} contract(s)  (1 contract covers 100 shares)")
    print(f"{'='*76}")
    print(f"""
  HOW A COVERED CALL WORKS:
  You already own the stock. You sell someone the RIGHT to buy it
  from you at the strike price by expiry.

  OUTCOME 1 -- Stock stays below strike (most common if you pick OTM):
    Call expires worthless. You keep your shares AND the premium.
    Your cost basis drops by the premium amount. Repeat next month.

  OUTCOME 2 -- Stock rises above strike (called away):
    Your shares get sold at the strike price. You keep the premium too.
    You still profit -- you sold at a price you were happy with.
    Then restart the wheel: sell a new cash-secured put.

  HVR {hv_rank:.0f}/100 -- {"GREAT time to sell calls, premium is elevated" if hv_rank >= 50 else "moderate -- premium is thinner but still works"}
""")

    # Filter liquid calls above current price
    calls = calls[
        (calls["bid"] > 0) &
        (calls["ask"] > 0) &
        (calls["openInterest"] > 500) &
        (calls["strike"] > current)
    ].reset_index(drop=True)

    results = []

    for _, row in calls.iterrows():
        strike = row["strike"]
        bid    = row["bid"]

        if strike > current * 1.25:
            continue

        premium_per_contract = bid * 100 * contracts
        new_cost_basis       = cost_basis - bid          # premium reduces cost basis
        upside_cap           = strike - current          # max stock gain before called away
        upside_cap_pct       = upside_cap / current
        total_return_if_called = ((strike - entry_price) + bid) / entry_price
        annualized_return    = (bid / current) * (365 / dte) * 100
        prob_keep_shares     = bs_prob_otm_call(current, strike, T, iv)

        results.append({
            "strike":          strike,
            "bid":             bid,
            "premium":         premium_per_contract,
            "new_cost_basis":  new_cost_basis,
            "upside_cap":      upside_cap_pct,
            "total_if_called": total_return_if_called,
            "ann_return":      annualized_return,
            "prob_keep":       prob_keep_shares,
        })

    if not results:
        print(f"  No covered call strikes found.")
        return

    results.sort(key=lambda x: x["ann_return"], reverse=True)

    tiers = [
        (current * 1.02, current * 1.07, "AGGRESSIVE  (2-7% OTM, high premium, more chance of being called away)"),
        (current * 1.07, current * 1.12, "MODERATE    (7-12% OTM, balanced)"),
        (current * 1.12, current * 1.25, "CONSERVATIVE (12%+ OTM, keep shares very likely, lower premium)"),
    ]

    for lo, hi, label in tiers:
        tier = [r for r in results if lo <= r["strike"] < hi]
        if not tier:
            continue
        best = sorted(tier, key=lambda x: x["ann_return"], reverse=True)[:2]
        print(f"  -- {label} --")
        for r in best:
            print(f"  Sell ${r['strike']:.0f} call  @ ${r['bid']:.2f}/share")
            print(f"    Premium collected  : ${r['premium']:.0f}  ({contracts} contract(s))")
            print(f"    New cost basis     : ${r['new_cost_basis']:.2f}/share  (premium reduces what you paid)")
            print(f"    If called away     : total return {r['total_if_called']:.1%} from entry")
            print(f"    Upside you give up : capped at {r['upside_cap']:.1%} above current price")
            print(f"    Annualized yield   : {r['ann_return']:.1f}%  |  Prob keeping shares: {r['prob_keep']:.1%}")
            print(f"    Entry cmd          : python positions.py add-option {ticker} "
                  f"{r['strike']:.0f} {expiry} call {contracts} {r['bid']:.2f}")
            print()

    print(f"{'='*76}")
    print(f"  COVERED CALL STRATEGY:")
    print(f"  - Sell 30-45 DTE calls each month for steady income")
    print(f"  - Close at 50% profit (don't hold to expiry)")
    print(f"  - If assigned: sell a new cash-secured put to re-enter the position")
    print(f"  - Target: 2-4% premium per month = 24-48% annualized income on your shares")
    print(f"  - Only sell calls on stocks you are HAPPY to sell at that strike price")
    print(f"{'='*76}\n")


if __name__ == "__main__":
    ticker      = sys.argv[1].upper() if len(sys.argv) > 1 else input("Ticker: ").strip().upper()
    entry_price = float(sys.argv[2])  if len(sys.argv) > 2 else float(input("Your entry price: ") or 0)
    shares      = int(sys.argv[3])    if len(sys.argv) > 3 else int(input("Shares owned: ") or 100)
    target_dte  = int(sys.argv[4])    if len(sys.argv) > 4 else 30
    analyze_covered_calls(ticker, entry_price, shares, target_dte)
