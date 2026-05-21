# -*- coding: utf-8 -*-
"""
Spread Finder — Bull Call Spreads + Bull Put Spreads
======================================================
Finds affordable bullish spreads within a given budget.

THE 4 SPREAD TYPES (for reference):
  BULLISH (stock goes up):
    [1] Bull Call Spread  -- DEBIT  -- buy lower call  + sell higher call
    [2] Bull Put Spread   -- CREDIT -- sell higher put + buy lower put

  BEARISH (stock goes down):
    [3] Bear Put Spread   -- DEBIT  -- buy higher put  + sell lower put
    [4] Bear Call Spread  -- CREDIT -- sell lower call + buy higher call

This tool only shows [1] and [2] since our strategy is BULLISH (golden cross).

Usage:
  python spread_finder.py NVDA              -- budget $500, 90 DTE
  python spread_finder.py NVDA 500          -- budget $500
  python spread_finder.py NVDA 1000 90      -- budget $1000, 90 DTE
"""
import sys
sys.path.insert(0, ".")

from datetime import date
from math import log, sqrt, exp
from scipy.stats import norm
import numpy as np
import pandas as pd

from data.fetcher import fetch_ohlcv, fetch_options_chain
from indicators.volatility import calculate_hv_rank, spread_recommendation


def bs_prob_itm(S, K, T, sigma):
    """Probability the stock closes above strike K at expiry."""
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    d2 = (log(S / K) + (-0.5 * sigma**2) * T) / (sigma * sqrt(T))
    return float(norm.cdf(d2))


def find_bull_call_spreads(calls, current, T, iv, budget, ticker, expiry, dte):
    """
    BULL CALL SPREAD (Debit)
    -------------------------
    You PAY to enter. You profit if the stock rises above your break-even.
    Buy a lower-strike call, sell a higher-strike call.
    Max loss = net debit paid. Max profit = spread width - net debit.
    Best when: you want leveraged upside with strictly capped downside.
    """
    results = []

    for i, buy_row in calls.iterrows():
        buy_strike = buy_row["strike"]
        buy_ask    = buy_row["ask"]

        if buy_strike < current * 0.98 or buy_strike > current * 1.12:
            continue

        for j, sell_row in calls.iterrows():
            if j <= i:
                continue

            sell_strike = sell_row["strike"]
            sell_bid    = sell_row["bid"]

            width = sell_strike - buy_strike
            if width < 5 or width > 25:
                continue

            net_cost   = (buy_ask - sell_bid) * 100
            if net_cost <= 0 or net_cost > budget:
                continue

            max_profit  = (width - (buy_ask - sell_bid)) * 100
            breakeven   = buy_strike + (buy_ask - sell_bid)
            prob_profit = bs_prob_itm(current, breakeven, T, iv)
            rr          = max_profit / net_cost

            results.append({
                "type":        "BULL CALL SPREAD (Debit)",
                "direction":   "Bullish -- profit if stock RISES above break-even",
                "entry":       "Pay $%.0f upfront" % net_cost,
                "exit_note":   "Max loss = $%.0f (what you paid). Nothing more." % net_cost,
                "buy_leg":     "Buy  $%.0f CALL @ ask $%.2f" % (buy_strike, buy_ask),
                "sell_leg":    "Sell $%.0f CALL @ bid $%.2f" % (sell_strike, sell_row["bid"]),
                "buy":         buy_strike,
                "sell":        sell_strike,
                "width":       width,
                "cost":        net_cost,
                "max_profit":  max_profit,
                "breakeven":   breakeven,
                "prob_profit": prob_profit,
                "rr":          rr,
                "add_cmd": (
                    f"python positions.py add-option {ticker} "
                    f"{buy_strike:.0f} {expiry} call 1 {net_cost/100:.2f}  (buy leg only)"
                ),
            })

    return results


def find_bull_put_spreads(puts, current, T, iv, budget, ticker, expiry, dte):
    """
    BULL PUT SPREAD (Credit)
    -------------------------
    You COLLECT premium to enter. You profit if stock stays ABOVE the short put.
    Sell a higher-strike put, buy a lower-strike put as protection.
    Max profit = credit received. Max loss = spread width - credit.
    Best when: you want to profit from time decay and a stable/rising stock.
    NOTE: requires the stock to NOT drop significantly. Less upside leverage
    than a bull call, but you win even if the stock barely moves.
    """
    results = []

    # Filter for liquid puts only
    liquid_puts = puts[
        (puts["bid"] > 0) &
        (puts["ask"] > 0) &
        (puts["openInterest"] > 100)
    ].reset_index(drop=True)

    for i, sell_row in liquid_puts.iterrows():
        sell_strike = sell_row["strike"]
        sell_bid    = sell_row["bid"]

        # Sell slightly OTM puts — 0-10% below current price
        if sell_strike > current * 1.01 or sell_strike < current * 0.90:
            continue

        for j, buy_row in liquid_puts.iterrows():
            if j >= i:
                continue

            buy_strike = buy_row["strike"]
            buy_ask    = buy_row["ask"]

            width = sell_strike - buy_strike
            if width < 5 or width > 25:
                continue

            net_credit = (sell_bid - buy_ask) * 100
            if net_credit <= 0:
                continue

            max_loss    = (width * 100) - net_credit
            if max_loss > budget:
                continue

            breakeven   = sell_strike - (sell_bid - buy_ask)
            prob_profit = bs_prob_itm(current, breakeven, T, iv)
            rr          = net_credit / max_loss  # credit / max loss

            results.append({
                "type":        "BULL PUT SPREAD (Credit)",
                "direction":   "Bullish -- profit if stock STAYS ABOVE break-even",
                "entry":       "Collect $%.0f upfront" % net_credit,
                "exit_note":   "Max loss = $%.0f if stock falls below $%.2f" % (max_loss, buy_strike),
                "buy_leg":     "Buy  $%.0f PUT  @ ask $%.2f  (protection)" % (buy_strike, buy_ask),
                "sell_leg":    "Sell $%.0f PUT  @ bid $%.2f  (income leg)" % (sell_strike, sell_bid),
                "buy":         buy_strike,
                "sell":        sell_strike,
                "width":       width,
                "cost":        max_loss,        # what you're risking
                "net_credit":  net_credit,
                "max_profit":  net_credit,
                "breakeven":   breakeven,
                "prob_profit": prob_profit,
                "rr":          rr,
                "add_cmd": (
                    f"python positions.py add-option {ticker} "
                    f"{sell_strike:.0f} {expiry} put 1 {net_credit/100:.2f}  (sell leg — enter as short put)"
                ),
            })

    return results


def print_spread_group(results, label, budget, ticker):
    tiers = [
        (0,      250,  "CHEAPEST (under $250 at risk)"),
        (250,    500,  "MODERATE ($250-500 at risk)"),
        (500,    800,  "MODERATE ($500-800 at risk)"),
        (800,    budget, f"HIGHER (up to ${budget:.0f} at risk)"),
    ]

    shown = set()
    any_shown = False

    for lo, hi, tier_label in tiers:
        tier = [r for r in results if lo < r["cost"] <= hi]
        if not tier:
            continue
        best = sorted(tier, key=lambda x: x["rr"], reverse=True)[:2]
        printed_tier = False
        for r in best:
            key = (r["buy"], r["sell"])
            if key in shown:
                continue
            shown.add(key)
            if not printed_tier:
                print(f"\n  -- {tier_label} --")
                printed_tier = True
            any_shown = True

            print(f"  Spread type : {r['type']}")
            print(f"  Direction   : {r['direction']}")
            print(f"  Legs        : {r['buy_leg']}")
            print(f"              : {r['sell_leg']}")
            print(f"  Entry       : {r['entry']}")
            print(f"  {r['exit_note']}")
            if "net_credit" in r:
                print(f"  Max profit  : ${r['max_profit']:.0f}  (keep full credit if stock stays above ${r['sell']:.0f})")
            else:
                print(f"  Max profit  : ${r['max_profit']:.0f}  (if {ticker} closes AT OR ABOVE ${r['sell']:.0f})")
            print(f"  Break even  : ${r['breakeven']:.2f}  |  Prob profit: {r['prob_profit']:.1%}  |  R/R: {r['rr']:.2f}x")
            print(f"  Entry cmd   : {r['add_cmd']}")
            print()

    if not any_shown:
        print(f"  No {label} found under ${budget:.0f} at risk. Try increasing your budget.")


def find_spreads(ticker: str, budget: float, target_dte: int = 90) -> None:
    print(f"\nFetching {ticker} chain...")
    df_price = fetch_ohlcv(ticker, "2024-01-01", str(date.today()))
    current  = float(df_price["Close"].iloc[-1])

    log_ret = np.log(df_price["Close"] / df_price["Close"].shift(1)).dropna()
    iv = float(log_ret.tail(30).std() * np.sqrt(252))

    hvr             = calculate_hv_rank(df_price)
    hv_rank         = hvr["hv_rank"]
    rec_type, rec_reason = spread_recommendation(hv_rank, 2)

    chain = fetch_options_chain(ticker, target_dte)
    if not chain or chain["calls"].empty:
        print("  No options data available.")
        return

    calls  = chain["calls"].copy()
    puts   = chain["puts"].copy()
    expiry = chain["expiry"]
    dte    = chain["dte"]
    T      = dte / 365

    # Expected Move — what the options market thinks the stock will move by expiry.
    # Formula: Stock Price x IV x sqrt(T)
    # This is the 1-standard-deviation range the market is pricing in.
    # ~68% chance the stock finishes inside this range at expiry.
    expected_move = iv * current * (T ** 0.5)
    em_pct        = expected_move / current

    # Filter calls for liquidity
    calls = calls[
        (calls["bid"] > 0) &
        (calls["ask"] > 0) &
        (calls["openInterest"] > 100)
    ].reset_index(drop=True)

    print(f"\n{'='*72}")
    print(f"  BULLISH SPREADS  --  {ticker}  |  Price: ${current:.2f}  |  Exp: {expiry} ({dte} DTE)")
    print(f"  Budget (max at risk): ${budget:.0f}  |  Each spread = 1 contract (100 shares)")
    print(f"{'='*72}")

    print(f"""
  THE 4 SPREAD TYPES (quick reference):
  -----------------------------------------------------------------------
  [1] BULL CALL SPREAD  (Debit,  Bullish) -- shown below
      Pay upfront. Profit if stock RISES past break-even.
      Best for: strong bullish conviction, leveraged upside.

  [2] BULL PUT SPREAD   (Credit, Bullish) -- shown below
      Collect premium. Profit if stock stays ABOVE the short put.
      Best for: moderately bullish or neutral -- win even if stock flat.

  [3] BEAR PUT SPREAD   (Debit,  Bearish) -- not shown (strategy is bullish)
      Pay upfront. Profit if stock FALLS below break-even.

  [4] BEAR CALL SPREAD  (Credit, Bearish) -- not shown (strategy is bullish)
      Collect premium. Profit if stock stays BELOW the short call.
  -----------------------------------------------------------------------
  Showing [1] and [2] only -- both are bullish, match our golden cross strategy.
""")
    print(f"  HV RANK (IV proxy): {hv_rank:.0f}/100  |  Current HV: {hvr['current_hv']:.1%}")
    print(f"  Expected Move by {expiry}: +/- ${expected_move:.2f}  ({em_pct:.1%} of stock price)")
    print(f"  Expected range: ${current - expected_move:.2f}  to  ${current + expected_move:.2f}")
    print(f"  --> Put credit short strike should ideally sit BELOW ${current - expected_move:.2f}")
    print(f"  DATA-DRIVEN RECOMMENDATION: {rec_type}")
    print(f"  Reason: {rec_reason}")
    print()

    # ---- Bull Call Spreads ----
    print(f"  {'='*68}")
    print(f"  [1] BULL CALL SPREADS (Debit) -- you PAY to enter")
    print(f"  {'='*68}")
    bc_results = find_bull_call_spreads(calls, current, T, iv, budget, ticker, expiry, dte)
    bc_results.sort(key=lambda x: x["rr"], reverse=True)
    print_spread_group(bc_results, "Bull Call Spreads", budget, ticker)

    # ---- Bull Put Spreads ----
    print(f"  {'='*68}")
    print(f"  [2] BULL PUT SPREADS (Credit) -- you COLLECT premium to enter")
    print(f"  {'='*68}")
    bp_results = find_bull_put_spreads(puts, current, T, iv, budget, ticker, expiry, dte)
    bp_results.sort(key=lambda x: x["rr"], reverse=True)
    print_spread_group(bp_results, "Bull Put Spreads", budget, ticker)

    print(f"\n{'='*72}")
    print(f"  WHICH TYPE SHOULD YOU USE?  (data-driven, not guesswork)")
    print(f"  Bull Call Spread -- pay upfront, profit if stock RISES significantly.")
    print(f"  Bull Put Spread  -- collect premium, profit if stock STAYS above short put.")
    print(f"\n  HV RANK GUIDE:")
    print(f"  HVR > 50  options expensive right now  -> SELL premium -> Bull Put Spread")
    print(f"  HVR < 30  options cheap right now      -> BUY options  -> Bull Call Spread")
    print(f"  HVR 30-50 moderate                     -> use signal strength to decide")
    print(f"\n  {ticker} HVR: {hv_rank:.0f}/100  -->  RECOMMENDED: {rec_type}")
    print(f"  {rec_reason}")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    ticker     = sys.argv[1].upper() if len(sys.argv) > 1 else input("Ticker: ").strip().upper()
    budget     = float(sys.argv[2])  if len(sys.argv) > 2 else float(input("Max budget / max at risk ($): ") or 500)
    target_dte = int(sys.argv[3])    if len(sys.argv) > 3 else 90

    find_spreads(ticker, budget, target_dte)
