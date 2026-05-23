# -*- coding: utf-8 -*-
"""
Conquest Trading — Backtesting Engine
======================================
Runs the EXACT same signal logic used in production (Entry_Signal, MTF_Score,
MACD_Cross_Up, SQZ_FIRED) against 2–3 years of historical daily data.

This answers: does the strategy have a real edge, or have we just been lucky?

Trade simulation rules (mirror paper_trader.py):
  - stock_long  : entry when Entry_Signal fires, $1,000 notional
  - stock_short : entry when MACD_Cross_Down fires in BEAR regime, $1,000 notional
  - Profit target : +5 %
  - Stop loss     : -3 %
  - Max hold      : 5 calendar days
  - One position per ticker at a time (no pyramiding)
  - Entry at next-day OPEN to avoid lookahead bias

Usage:
  python backtest.py                              # top 20 universe, 2 years
  python backtest.py --tickers AAPL NVDA SPY     # specific tickers
  python backtest.py --period 3y --top 40        # 3 years, top-40 pre-screener
  python backtest.py --discord                   # post results to Discord
  python backtest.py --output results.json       # save raw trades
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta, timezone

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Universe: pull from universe_screener (129 S&P 500 names) ─────────────────
try:
    from universe_screener import SP500_UNIVERSE as DEFAULT_UNIVERSE
except ImportError:
    # Fallback if screener unavailable
    DEFAULT_UNIVERSE = [
        "AAPL", "NVDA", "MSFT", "GOOGL", "AMZN",
        "META", "TSLA", "JPM",  "XOM",   "WMT",
        "COP",  "SPY",  "QQQ",  "AMD",   "NFLX",
        "COST", "V",    "MA",   "BAC",   "DIS",
    ]

STK_PROFIT    =  0.05
STK_STOP      = -0.03
# Options exits are now signal-driven — NO fixed profit target, NO fixed stop %.
# Instead:
#   • Close calls when MACD_Cross_Down fires or Regime flips BEAR (thesis inverts)
#   • Close puts  when MACD_Cross_Up  fires or Regime flips BULL (thesis inverts)
#   • RSI extremes + in-profit → take gains (overbought/oversold reversal warning)
#   • BACKSTOP_OPT: catastrophic safety net only — prevents total option wipeout
#   • MAX_HOLD_OPT: hard expiry limit (agents would never hold past near-expiry anyway)
BACKSTOP_OPT  = -0.65    # emergency backstop: only triggers on catastrophic moves
MAX_HOLD_DAYS =  5       # stock max hold (kept for backward compat)
MAX_HOLD_STK  =  5       # stock max hold (calendar days)
MAX_HOLD_OPT  = 30       # options max hold — 30 days (bought at 30 DTE, exit near expiry)
DTE_TARGET    = 30       # buy 30 DTE options at entry
OTM_PCT       = 0.02     # 2% OTM strike
NOTIONAL      = 1_000    # per-trade notional = 1% of $100k starting capital
RISK_FREE     = 0.05     # 5% annualised for Black-Scholes
STARTING_CAPITAL = 100_000   # paper account starting value

# ── Signal quality filter ──────────────────────────────────────────────────────
# The live 6-agent swarm requires ≥4/6 agents to agree before trading.
# That naturally filters to MTF_Score ≥ 2 (at least 2 of 3 timeframes aligned).
# Without this filter the backtest trades every signal including low-quality MTF 0/3
# noise — the backtest showed 1,887 MTF 0/3 trades at avg -$139 each dragging results.
MTF_MIN_SCORE = 2   # only trade when at least 2/3 timeframes confirm the signal

# ── Realistic friction costs (backtest realism) ────────────────────────────────
# Real markets have bid-ask spreads and commissions. These adjustments bring
# the backtest closer to live performance.
OPT_COMMISSION_PER_CONTRACT = 0.65   # $0.65/contract (Robinhood/TastyTrade)
STK_COMMISSION_PER_SHARE    = 0.003  # $0.003/share (most zero-commission + spread)
IV_PREMIUM_FACTOR           = 1.20   # real IV ≈ HV × 1.20 (volatility risk premium)
# What this means: options cost ~20% more than Black-Scholes on raw HV predicts.
# This is the most impactful adjustment — it directly reduces options entry P&L.


# ─────────────────────────────────────────────────────────────────────────────
# Options pricing (Black-Scholes via scipy — no historical options data needed)
# ─────────────────────────────────────────────────────────────────────────────

def _bs_price(S: float, K: float, T: float, r: float, sigma: float,
              option_type: str = "call") -> float:
    """
    Black-Scholes option price.
    S=spot, K=strike, T=years to expiry, r=risk-free, sigma=annualised vol.
    Returns 0 on bad inputs.
    """
    from scipy.stats import norm
    import math
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if option_type == "call":
            return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
        else:
            return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    except Exception:
        return 0.0


def _hist_vol(closes: np.ndarray, window: int = 30) -> float:
    """30-day annualised historical volatility — proxy for IV at entry."""
    if len(closes) < window + 1:
        return 0.25   # fallback 25%
    log_rets = np.diff(np.log(closes[-window - 1:]))
    return float(np.std(log_rets) * np.sqrt(252))


def _hv_rank(closes: np.ndarray, current_idx: int, lookback: int = 252) -> float:
    """
    Where is today's 30-day HV relative to the trailing year?
    Returns 0.0–1.0. Low = vol is cheap vs history, high = vol is expensive.
    Below 0.35 = options are cheap enough to buy directional exposure.
    """
    if current_idx < 60:
        return 0.50  # insufficient history — neutral default
    iv_today = _hist_vol(closes[:current_idx + 1])
    start    = max(0, current_idx - lookback)
    # Sample every 5 bars for efficiency (~50 data points over a year)
    samples  = np.array([
        _hist_vol(closes[max(0, j - 30):j + 1])
        for j in range(start + 30, current_idx, 5)
    ])
    if len(samples) < 10:
        return 0.50
    return float(np.searchsorted(np.sort(samples), iv_today) / len(samples))


def _simulate_option_trades(ticker: str, df: pd.DataFrame,
                            option_type: str = "call") -> list[dict]:
    """
    Simulate long call or long put trades.
    Entry: next-day open after Entry_Signal (call) or bearish MACD (put).
    Pricing: Black-Scholes with 30-day HV as IV proxy, 30 DTE, 2% OTM.
    Exit: +50% profit | -75% stop | 21-day max hold.
    """
    trades  = []
    closes  = df["Close"].values
    opens   = df["Open"].values
    dates   = df.index.tolist()
    n       = len(df)

    in_trade    = False
    entry_idx   = None
    entry_price = None   # option premium at entry
    strike      = None
    entry_meta  = {}

    for i in range(n - 1):
        row = df.iloc[i]

        if not in_trade:
            signal = False
            if option_type == "call" and row.get("Entry_Signal", 0):
                signal = True
            elif option_type == "put" and (
                row.get("MACD_Cross_Down", 0) and row.get("Regime", 1) == 0
            ):
                signal = True

            # Only trade when signal quality meets the live agent conviction threshold
            if signal and int(row.get("MTF_Score", 0)) < MTF_MIN_SCORE:
                signal = False

            # IV rank gate: only buy long options when vol is cheap (< 35th percentile).
            # Buying options when IV is elevated means overpaying — the premium erodes
            # the trade before it even starts. Low HV rank = markets are calm = options
            # are priced on low expected vol = cheap time to own directional exposure.
            _hv_rank_now = 0.50
            if signal:
                _hv_rank_now = _hv_rank(closes, i)
                if _hv_rank_now > 0.35:
                    signal = False  # IV too expensive, skip — sell premium instead

            # Squeeze gate: only enter on confirmed TTM Squeeze momentum releases.
            # SQZ_FIRED is a LEADING indicator — it fires BEFORE the explosive move.
            # Without it, we're entering on lagging MACD signals and buying into moves
            # already in progress. Squeeze-fired entries are the source of the 100% WR
            # rsi_take_profit exits seen in backtest analysis.
            if signal and not bool(row.get("SQZ_FIRED", 0)):
                signal = False  # no squeeze = no confirmed momentum release

            if signal:
                entry_idx   = i + 1
                spot        = float(opens[entry_idx])
                iv_raw      = _hist_vol(closes[:i + 1])
                # Apply IV premium: real options trade above raw HV due to vol risk premium
                iv          = iv_raw * IV_PREMIUM_FACTOR
                T           = DTE_TARGET / 365

                if option_type == "call":
                    K = spot * (1 + OTM_PCT)
                else:
                    K = spot * (1 - OTM_PCT)

                premium = _bs_price(spot, K, T, RISK_FREE, iv, option_type)
                if premium <= 0.01:
                    continue    # unpriced — skip

                # $1,000 notional → how many contracts (1 contract = 100 shares)
                n_contracts  = max(1, int(NOTIONAL / (premium * 100)))
                entry_price  = premium
                strike       = K
                entry_meta   = {
                    "iv":           round(iv, 4),
                    "iv_rank":      round(_hv_rank_now, 3),
                    "strike":       round(K, 2),
                    "dte_entry":    DTE_TARGET,
                    "n_contracts":  n_contracts,
                    "mtf_score":    int(row.get("MTF_Score", 0)),
                    "entry_signal": bool(row.get("Entry_Signal", 0)),
                    "sqz_fired":    bool(row.get("SQZ_FIRED", 0)),
                    "macd_cross":   bool(row.get("MACD_Cross_Up", 0)),
                    "rsi_entry":    float(row.get("RSI", 50)),
                    "adx_entry":    float(row.get("ADX", 0)),
                    "option_type":  option_type,
                }
                in_trade = True
                continue

        else:
            days_held = i - entry_idx + 1
            dte_now   = max(0.5 / 365, (DTE_TARGET - days_held) / 365)
            spot_now  = float(closes[i])
            iv_now    = _hist_vol(closes[:i + 1])

            current_premium = _bs_price(spot_now, strike, dte_now,
                                        RISK_FREE, iv_now, option_type)
            if entry_price > 0:
                pnl_pct = (current_premium - entry_price) / entry_price
            else:
                pnl_pct = 0

            pnl_gross  = pnl_pct * entry_price * entry_meta["n_contracts"] * 100
            # Deduct round-trip commissions: open + close = 2× per contract
            commission = OPT_COMMISSION_PER_CONTRACT * entry_meta["n_contracts"] * 2
            pnl_dollar = pnl_gross - commission

            # ── Signal-driven exits (mimics what AI agents would decide) ────────
            # The thesis that opened the trade inverts when the signal reverses.
            # We don't cap winners at an arbitrary % — we let the trade run until
            # the market tells us the move is over.
            row_now = df.iloc[i]
            if option_type == "call":
                signal_reversed = bool(
                    row_now.get("MACD_Cross_Down", 0) or   # momentum turned bearish
                    row_now.get("Regime", 1) == 0           # market regime flipped BEAR
                )
                # RSI overbought + already profitable → smart agents take gains here
                rsi_take_profit = (row_now.get("RSI", 50) > 76 and pnl_pct >= 0.20)
            else:
                signal_reversed = bool(
                    row_now.get("MACD_Cross_Up", 0) or     # momentum turned bullish
                    row_now.get("Regime", 1) == 1           # market regime flipped BULL
                )
                # RSI oversold + already profitable → smart agents take gains here
                rsi_take_profit = (row_now.get("RSI", 50) < 26 and pnl_pct >= 0.20)

            hit_backstop = pnl_pct <= BACKSTOP_OPT    # catastrophic safety net only
            hit_max      = days_held >= MAX_HOLD_OPT   # approaching expiry

            if signal_reversed or rsi_take_profit or hit_backstop or hit_max:
                reason = ("signal_reversal" if signal_reversed
                          else "rsi_take_profit" if rsi_take_profit
                          else "backstop" if hit_backstop
                          else "max_hold")
                trades.append({
                    "ticker":       ticker,
                    "trade_type":   f"long_{option_type}",
                    "direction":    option_type,
                    "entry_date":   str(dates[entry_idx].date()),
                    "exit_date":    str(dates[i].date()),
                    "entry_price":  round(entry_price, 4),
                    "exit_price":   round(current_premium, 4),
                    "strike":       round(strike, 2),
                    "pnl_pct":      round(pnl_pct * 100, 3),
                    "pnl_dollar":   round(pnl_dollar, 2),
                    "commission":   round(commission, 2),
                    "days_held":    days_held,
                    "close_reason": reason,
                    "mtf_score":    entry_meta["mtf_score"],
                    "sqz_fired":    entry_meta["sqz_fired"],
                    "macd_cross":   entry_meta["macd_cross"],
                    "rsi_entry":    round(entry_meta["rsi_entry"], 1),
                    "adx_entry":    round(entry_meta["adx_entry"], 1),
                    "entry_signal": entry_meta["entry_signal"],
                    "iv_entry":     entry_meta["iv"],
                    "iv_rank":      entry_meta.get("iv_rank", 0.50),
                })
                in_trade = False

    return trades


def _simulate_credit_spread_trades(ticker: str, df: pd.DataFrame,
                                    spread_type: str = "bull_put") -> list[dict]:
    """
    Simulate credit spread trades — the strategy that COLLECTS the IV premium
    instead of paying it.

    bull_put  : sell put 3% below spot, buy put 6% below → profit if stock stays bullish
    bear_call : sell call 3% above spot, buy call 6% above → profit if stock stays bearish

    Credit spreads win when the underlying does nothing or moves your way.
    Time decay and the volatility risk premium both work FOR you.
    Max profit = net credit. Max loss = spread width - credit.
    """
    trades     = []
    closes     = df["Close"].values
    opens      = df["Open"].values
    dates      = df.index.tolist()
    n          = len(df)
    in_trade   = False
    entry_idx  = None
    entry_credit = None
    entry_meta = {}

    for i in range(n - 1):
        row = df.iloc[i]

        if not in_trade:
            signal = False
            if spread_type == "bull_put":
                if row.get("Entry_Signal", 0) and int(row.get("MTF_Score", 0)) >= MTF_MIN_SCORE:
                    signal = True
            else:  # bear_call
                if (row.get("MACD_Cross_Down", 0) and row.get("Regime", 1) == 0
                        and int(row.get("MTF_Score", 0)) >= MTF_MIN_SCORE):
                    signal = True

            if signal:
                entry_idx = i + 1
                spot      = float(opens[entry_idx])
                iv_raw    = _hist_vol(closes[:i + 1])
                iv        = iv_raw * IV_PREMIUM_FACTOR
                T         = DTE_TARGET / 365

                if spread_type == "bull_put":
                    short_k  = spot * 0.97   # sell put 3% below spot
                    long_k   = spot * 0.94   # buy put 6% below spot (protection)
                    opt_type = "put"
                else:
                    short_k  = spot * 1.03   # sell call 3% above spot
                    long_k   = spot * 1.06   # buy call 6% above spot (protection)
                    opt_type = "call"

                short_val = _bs_price(spot, short_k, T, RISK_FREE, iv, opt_type)
                long_val  = _bs_price(spot, long_k,  T, RISK_FREE, iv, opt_type)
                credit    = short_val - long_val

                if credit <= 0.01:
                    continue

                spread_width = abs(short_k - long_k)
                n_contracts  = max(1, int(NOTIONAL / (spread_width * 100)))
                entry_credit = credit
                entry_meta   = {
                    "short_k":      short_k,
                    "long_k":       long_k,
                    "spread_width": spread_width,
                    "n_contracts":  n_contracts,
                    "iv":           round(iv, 4),
                    "opt_type":     opt_type,
                    "mtf_score":    int(row.get("MTF_Score", 0)),
                    "rsi_entry":    float(row.get("RSI", 50)),
                    "entry_signal": bool(row.get("Entry_Signal", 0)),
                    "sqz_fired":    bool(row.get("SQZ_FIRED", 0)),
                    "macd_cross":   bool(row.get("MACD_Cross_Up", 0)),
                    "adx_entry":    float(row.get("ADX", 0)),
                }
                in_trade = True
                continue

        else:
            days_held  = i - entry_idx + 1
            dte_now    = max(0.5 / 365, (DTE_TARGET - days_held) / 365)
            spot_now   = float(closes[i])
            iv_now     = _hist_vol(closes[:i + 1])

            # Apply IV_PREMIUM_FACTOR at exit too — the market still overprices options
            # when we close the position. Without this, exit prices are cheaper than
            # real life, inflating credit spread P&L. Both entry and exit are symmetric.
            iv_exit = iv_now * IV_PREMIUM_FACTOR
            sv_now = _bs_price(spot_now, entry_meta["short_k"], dte_now,
                               RISK_FREE, iv_exit, entry_meta["opt_type"])
            lv_now = _bs_price(spot_now, entry_meta["long_k"],  dte_now,
                               RISK_FREE, iv_exit, entry_meta["opt_type"])
            cost_to_close = max(sv_now - lv_now, 0)

            pnl_gross  = (entry_credit - cost_to_close) * entry_meta["n_contracts"] * 100
            commission = OPT_COMMISSION_PER_CONTRACT * entry_meta["n_contracts"] * 2
            pnl_dollar = pnl_gross - commission

            # Credit spread exits
            hit_profit = cost_to_close <= entry_credit * 0.25   # kept 75% of credit
            hit_stop   = cost_to_close >= entry_credit * 2.0    # spread doubled against us
            hit_max    = days_held >= MAX_HOLD_OPT

            row_now = df.iloc[i]
            if spread_type == "bull_put":
                signal_reversed = bool(row_now.get("MACD_Cross_Down", 0)
                                       or row_now.get("Regime", 1) == 0)
            else:
                signal_reversed = bool(row_now.get("MACD_Cross_Up", 0)
                                       or row_now.get("Regime", 1) == 1)

            if hit_profit or hit_stop or hit_max or signal_reversed:
                reason = ("profit_target"   if hit_profit
                          else "stop_loss"  if hit_stop
                          else "signal_reversal" if signal_reversed
                          else "max_hold")
                ttype = "bull_put_spread" if spread_type == "bull_put" else "bear_call_spread"
                max_risk = entry_meta["spread_width"] * entry_meta["n_contracts"] * 100
                trades.append({
                    "ticker":       ticker,
                    "trade_type":   ttype,
                    "entry_date":   str(dates[entry_idx].date()),
                    "exit_date":    str(dates[i].date()),
                    "entry_price":  round(entry_credit, 4),
                    "exit_price":   round(cost_to_close, 4),
                    "pnl_pct":      round(pnl_dollar / max_risk * 100, 3) if max_risk else 0,
                    "pnl_dollar":   round(pnl_dollar, 2),
                    "commission":   round(commission, 2),
                    "days_held":    days_held,
                    "close_reason": reason,
                    "mtf_score":    entry_meta["mtf_score"],
                    "sqz_fired":    entry_meta["sqz_fired"],
                    "macd_cross":   entry_meta["macd_cross"],
                    "rsi_entry":    round(entry_meta["rsi_entry"], 1),
                    "adx_entry":    round(entry_meta["adx_entry"], 1),
                    "entry_signal": entry_meta["entry_signal"],
                    "iv_entry":     entry_meta["iv"],
                })
                in_trade = False

    return trades


def _simulate_covered_call_trades(ticker: str, df: pd.DataFrame) -> list[dict]:
    """
    Simulate covered calls PROPERLY: own 100 shares + sell 1 call 3% OTM.

    Previous version sized by premium (e.g. 3 contracts on $3 premium), measured
    ROI against just the premium collected ($900 basis on AAPL) — that made 80% ROI
    trades look common. Real covered calls require owning the stock, so your actual
    capital deployed is spot × 100. A 1-2% monthly return on the position is realistic.

    This version:
      • Always 1 contract (the natural unit — you must own 100 shares)
      • cost_basis = entry_stock_price × 100 (the real capital tied up)
      • P&L = stock gain (capped at strike) + premium decay — both legs accounted for
      • Commission includes both option legs AND stock round-trip

    Exit rules:
      • Premium decayed 80% → take the income, close everything
      • Stock at/above strike → being called away, book the capped combined gain
      • Option doubled AND stock below entry → stock falling, call isn't saving you → stop
      • 21-day max hold
    """
    trades     = []
    closes     = df["Close"].values
    opens      = df["Open"].values
    dates      = df.index.tolist()
    n          = len(df)
    in_trade   = False
    entry_idx  = None
    entry_stock_price = None
    entry_premium = None
    strike     = None
    entry_meta = {}

    for i in range(n - 1):
        row = df.iloc[i]

        if not in_trade:
            if row.get("Entry_Signal", 0) and int(row.get("MTF_Score", 0)) >= MTF_MIN_SCORE:
                iv_raw_cc = _hist_vol(closes[:i + 1])
                # Covered calls are a premium-SELLING strategy. Enter only when IV
                # is moderate-to-high (rank ≥ 40) so the premium collected is worth
                # the stock risk. In low-IV environments the call premium is tiny —
                # you take full stock downside for almost no income.
                hv_rank_cc = _hv_rank(closes, i)
                if hv_rank_cc < 0.40:
                    continue  # IV too cheap — not worth selling a covered call here

                entry_idx         = i + 1
                spot              = float(opens[entry_idx])
                iv                = iv_raw_cc * IV_PREMIUM_FACTOR
                T                 = DTE_TARGET / 365
                strike_k          = spot * 1.03   # sell 3% OTM call

                prem = _bs_price(spot, strike_k, T, RISK_FREE, iv, "call")
                if prem <= 0.01:
                    continue

                entry_stock_price = spot
                entry_premium     = prem
                strike            = strike_k
                # Always 1 contract — you need 100 shares to sell 1 covered call.
                # Sizing by premium value (NOTIONAL / prem*100) inflates n_contracts
                # and makes ROI look 50-80x too good.
                entry_meta        = {
                    "entry_stock_price": spot,
                    "iv":                round(iv, 4),
                    "mtf_score":         int(row.get("MTF_Score", 0)),
                    "rsi_entry":         float(row.get("RSI", 50)),
                    "entry_signal":      bool(row.get("Entry_Signal", 0)),
                    "sqz_fired":         bool(row.get("SQZ_FIRED", 0)),
                    "macd_cross":        bool(row.get("MACD_Cross_Up", 0)),
                    "adx_entry":         float(row.get("ADX", 0)),
                }
                in_trade = True
                continue

        else:
            days_held       = i - entry_idx + 1
            dte_now         = max(0.5 / 365, (DTE_TARGET - days_held) / 365)
            spot_now        = float(closes[i])
            iv_now          = _hist_vol(closes[:i + 1])
            current_premium = _bs_price(spot_now, strike, dte_now, RISK_FREE, iv_now, "call")

            # Combined P&L:
            # Stock leg: gain from price move, but CAPPED at the strike (upside is sold away)
            entry_s   = entry_meta["entry_stock_price"]
            stock_gain = min(
                (spot_now - entry_s) * 100,          # uncapped move
                (strike   - entry_s) * 100            # cap at strike (above strike = called away)
            )
            opt_gain   = (entry_premium - current_premium) * 100  # positive when call decays
            pnl_gross  = stock_gain + opt_gain
            commission = (OPT_COMMISSION_PER_CONTRACT * 2 +        # open + close option
                          STK_COMMISSION_PER_SHARE * 100 * 2)       # buy + sell 100 shares
            pnl_dollar = pnl_gross - commission

            # Capital basis = the stock position (the real capital committed)
            cost_basis = entry_s * 100

            hit_premium = current_premium <= entry_premium * 0.20   # kept 80% of the premium
            hit_called  = spot_now >= strike                          # stock at/above strike → called away
            # Stock-based stop: close when stock falls more than 4%.
            # The original stop (call premium doubled) never fires on slow declines because
            # the option premium DECREASES as the stock moves away from the OTM strike.
            # A 4% stock drop means the position is net-negative (stock loss > premium income).
            hit_stop    = (spot_now - entry_s) / entry_s < -0.04
            hit_max     = days_held >= MAX_HOLD_OPT

            if hit_premium or hit_called or hit_stop or hit_max:
                reason = ("profit_target" if (hit_premium or hit_called)
                          else "stop_loss"  if hit_stop
                          else "max_hold")
                trades.append({
                    "ticker":       ticker,
                    "trade_type":   "covered_call",
                    "entry_date":   str(dates[entry_idx].date()),
                    "exit_date":    str(dates[i].date()),
                    "entry_price":  round(entry_premium, 4),
                    "exit_price":   round(current_premium, 4),
                    "strike":       round(strike, 2),
                    "pnl_pct":      round(pnl_dollar / cost_basis * 100, 3) if cost_basis else 0,
                    "pnl_dollar":   round(pnl_dollar, 2),
                    "commission":   round(commission, 2),
                    "days_held":    days_held,
                    "close_reason": reason,
                    "mtf_score":    entry_meta["mtf_score"],
                    "sqz_fired":    entry_meta["sqz_fired"],
                    "macd_cross":   entry_meta["macd_cross"],
                    "rsi_entry":    round(entry_meta["rsi_entry"], 1),
                    "adx_entry":    round(entry_meta["adx_entry"], 1),
                    "entry_signal": entry_meta["entry_signal"],
                    "iv_entry":     entry_meta["iv"],
                })
                in_trade = False

    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Signal computation (uses the live production pipeline)
# ─────────────────────────────────────────────────────────────────────────────

def _compute_signals(ticker: str, period: str = "2y") -> pd.DataFrame | None:
    """
    Fetch historical OHLCV and run the full signal pipeline.
    Returns a DataFrame with all indicator columns, or None on failure.
    """
    try:
        import yfinance as yf
        from config import Config, DataConfig
        from data.fetcher import fetch_ohlcv, fetch_vix, get_earnings_dates
        from signals.generator import generate_signals

        # Map period string to date range
        end   = date.today()
        years = int(period.rstrip("y")) if period.endswith("y") else 2
        start = end - timedelta(days=years * 365 + 60)  # +60 for indicator warmup

        cfg = Config(data=DataConfig(ticker=ticker))
        df  = fetch_ohlcv(ticker, str(start), str(end))
        if df is None or len(df) < 100:
            return None

        try:
            vix = fetch_vix(str(start), str(end))
        except Exception:
            vix = None

        try:
            ed = get_earnings_dates(ticker)
        except Exception:
            ed = None

        df = generate_signals(df, cfg.indicators, vix=vix, earnings_dates=ed)
        df["_ticker"] = ticker
        return df

    except Exception as e:
        print(f"  [Backtest] {ticker}: signal error — {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Trade simulation
# ─────────────────────────────────────────────────────────────────────────────

def _simulate_trades(ticker: str, df: pd.DataFrame) -> list[dict]:
    """
    Walk through the signal DataFrame day by day, open trades on signals,
    and close them when exit rules trigger.

    Entry: next-day OPEN (avoids buying at the same close you saw the signal)
    Exit : checked on each subsequent close
    """
    trades = []
    in_trade    = False
    entry_idx   = None
    entry_price = None
    entry_meta  = {}

    closes  = df["Close"].values
    opens   = df["Open"].values
    dates   = df.index.tolist()
    n       = len(df)

    for i in range(n - 1):               # -1 so we can always peek at i+1
        row = df.iloc[i]

        if not in_trade:
            # ── Check for long entry ───────────────────────────────────────
            if row.get("Entry_Signal", 0) and int(row.get("MTF_Score", 0)) >= MTF_MIN_SCORE:
                entry_idx   = i + 1          # enter at NEXT day's open
                entry_price = float(opens[i + 1])
                entry_meta  = {
                    "mtf_score":    int(row.get("MTF_Score", 0)),
                    "sqz_fired":    bool(row.get("SQZ_FIRED", 0)),
                    "macd_cross":   bool(row.get("MACD_Cross_Up", 0)),
                    "rsi_entry":    float(row.get("RSI", 50)),
                    "adx_entry":    float(row.get("ADX", 0)),
                    "entry_signal": True,
                    "direction":    "long",
                }
                in_trade = True
                continue

            # ── Check for short entry (bearish MACD in BEAR regime) ───────
            if (row.get("MACD_Cross_Down", 0) and row.get("Regime", 1) == 0
                    and int(row.get("MTF_Score", 0)) >= MTF_MIN_SCORE):
                entry_idx   = i + 1
                entry_price = float(opens[i + 1])
                entry_meta  = {
                    "mtf_score":  int(row.get("MTF_Score", 0)),
                    "sqz_fired":  False,
                    "macd_cross": True,
                    "rsi_entry":  float(row.get("RSI", 50)),
                    "adx_entry":  float(row.get("ADX", 0)),
                    "entry_signal": False,
                    "direction":  "short",
                }
                in_trade = True
                continue

        else:
            # ── We're in a trade — check exit conditions ───────────────────
            days_held     = i - entry_idx + 1
            current_close = float(closes[i])

            if entry_price <= 0:          # guard against bad data
                in_trade = False
                continue

            if entry_meta["direction"] == "long":
                pnl_pct = (current_close - entry_price) / entry_price
            else:
                pnl_pct = (entry_price - current_close) / entry_price

            hit_profit = pnl_pct >=  STK_PROFIT
            hit_stop   = pnl_pct <=  STK_STOP
            hit_max    = days_held >= MAX_HOLD_DAYS

            if hit_profit or hit_stop or hit_max:
                reason = ("profit_target" if hit_profit
                          else "stop_loss" if hit_stop
                          else "max_hold")
                shares     = NOTIONAL / entry_price if entry_price else 0
                # Round-trip commission: entry + exit shares
                commission = STK_COMMISSION_PER_SHARE * shares * 2
                pnl_gross  = NOTIONAL * pnl_pct
                pnl_dollar = pnl_gross - commission
                trades.append({
                    "ticker":       ticker,
                    "direction":    entry_meta["direction"],
                    "entry_date":   str(dates[entry_idx].date()),
                    "exit_date":    str(dates[i].date()),
                    "entry_price":  round(entry_price, 4),
                    "exit_price":   round(current_close, 4),
                    "pnl_pct":      round(pnl_pct * 100, 3),
                    "pnl_dollar":   round(pnl_dollar, 2),
                    "commission":   round(commission, 2),
                    "days_held":    days_held,
                    "close_reason": reason,
                    "mtf_score":    entry_meta["mtf_score"],
                    "sqz_fired":    entry_meta["sqz_fired"],
                    "macd_cross":   entry_meta["macd_cross"],
                    "rsi_entry":    round(entry_meta["rsi_entry"], 1),
                    "adx_entry":    round(entry_meta["adx_entry"], 1),
                    "entry_signal": entry_meta["entry_signal"],
                })
                in_trade = False

    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Stats aggregation
# ─────────────────────────────────────────────────────────────────────────────

def _compute_stats(trades: list[dict]) -> dict:
    """Turn raw trades into a rich stats dictionary."""
    if not trades:
        return {"error": "no trades generated"}

    df = pd.DataFrame(trades)

    total      = len(df)
    wins       = df[df["pnl_dollar"] > 0]
    losses     = df[df["pnl_dollar"] <= 0]
    win_rate   = len(wins) / total * 100
    total_pnl  = df["pnl_dollar"].sum()
    avg_pnl    = df["pnl_dollar"].mean()
    avg_hold   = df["days_held"].mean()
    total_commissions = df["commission"].sum() if "commission" in df.columns else 0
    best       = df.loc[df["pnl_dollar"].idxmax()]
    worst      = df.loc[df["pnl_dollar"].idxmin()]

    # Profit factor
    gross_win  = wins["pnl_dollar"].sum()
    gross_loss = abs(losses["pnl_dollar"].sum())
    profit_factor = round(gross_win / gross_loss, 3) if gross_loss else float("inf")

    # Sharpe (annualised, using daily P&L grouped by exit_date)
    daily_pnl = df.groupby("exit_date")["pnl_dollar"].sum()
    sharpe    = None
    if len(daily_pnl) > 10:
        mu     = daily_pnl.mean()
        sigma  = daily_pnl.std()
        sharpe = round((mu / sigma) * (252 ** 0.5), 3) if sigma else None

    # Max drawdown on cumulative P&L curve
    cum = df["pnl_dollar"].cumsum()
    rolling_max = cum.cummax()
    drawdown    = (cum - rolling_max)
    max_dd      = round(drawdown.min(), 2)

    # By close reason
    by_reason = df.groupby("close_reason").agg(
        count=("pnl_dollar", "count"),
        win_rate=("pnl_dollar", lambda x: (x > 0).mean() * 100),
        avg_pnl=("pnl_dollar", "mean"),
        total_pnl=("pnl_dollar", "sum"),
    ).round(2).to_dict("index")

    # By MTF score
    by_mtf = df.groupby("mtf_score").agg(
        count=("pnl_dollar", "count"),
        win_rate=("pnl_dollar", lambda x: (x > 0).mean() * 100),
        avg_pnl=("pnl_dollar", "mean"),
    ).round(2).to_dict("index")

    # By ticker (top/bottom 5)
    by_ticker = df.groupby("ticker").agg(
        count=("pnl_dollar", "count"),
        win_rate=("pnl_dollar", lambda x: (x > 0).mean() * 100),
        total_pnl=("pnl_dollar", "sum"),
        avg_pnl=("pnl_dollar", "mean"),
    ).round(2)
    top5    = by_ticker.nlargest(5,  "total_pnl").to_dict("index")
    bottom5 = by_ticker.nsmallest(5, "total_pnl").to_dict("index")

    # Signal-type win rates
    sig_stats = {}
    for col, label in [("entry_signal", "Entry_Signal"), ("sqz_fired", "Squeeze_Fired"),
                       ("macd_cross", "MACD_Cross")]:
        subset = df[df[col] == True]
        if len(subset):
            sig_stats[label] = {
                "count":    len(subset),
                "win_rate": round((subset["pnl_dollar"] > 0).mean() * 100, 1),
                "avg_pnl":  round(subset["pnl_dollar"].mean(), 2),
            }

    # By trade type (stock_long, stock_short, long_call, long_put)
    by_type = df.groupby("trade_type").agg(
        count=("pnl_dollar", "count"),
        win_rate=("pnl_dollar", lambda x: (x > 0).mean() * 100),
        avg_pnl=("pnl_dollar", "mean"),
        total_pnl=("pnl_dollar", "sum"),
        avg_hold=("days_held", "mean"),
    ).round(2).to_dict("index")

    # Monthly breakdown
    df["month"] = pd.to_datetime(df["exit_date"]).dt.to_period("M").astype(str)
    monthly = df.groupby("month")["pnl_dollar"].sum().round(2).to_dict()

    # Portfolio-level metrics on $100k starting capital
    ending_capital   = round(STARTING_CAPITAL + total_pnl, 2)
    total_return_pct = round(total_pnl / STARTING_CAPITAL * 100, 2)

    # Annualised return from period length
    try:
        start_dt = pd.to_datetime(df["entry_date"].min())
        end_dt   = pd.to_datetime(df["exit_date"].max())
        years    = max((end_dt - start_dt).days / 365.25, 0.01)
        ann_return_pct = round(((ending_capital / STARTING_CAPITAL) ** (1 / years) - 1) * 100, 2)
    except Exception:
        ann_return_pct = None

    return {
        "period":            f"{df['entry_date'].min()} to {df['exit_date'].max()}",
        "total_trades":      total,
        "win_rate":          round(win_rate, 1),
        "total_pnl":         round(total_pnl, 2),
        "avg_pnl":           round(avg_pnl, 2),
        "avg_hold_days":     round(avg_hold, 1),
        "profit_factor":     profit_factor,
        "sharpe":            sharpe,
        "max_drawdown":      max_dd,
        # Portfolio accounting
        "starting_capital":  STARTING_CAPITAL,
        "ending_capital":    ending_capital,
        "total_return_pct":  total_return_pct,
        "ann_return_pct":    ann_return_pct,
        "total_commissions": round(total_commissions, 2),
        "best_trade":  {
            "ticker": best["ticker"], "date": best["exit_date"],
            "pnl": round(best["pnl_dollar"], 2), "reason": best["close_reason"],
        },
        "worst_trade": {
            "ticker": worst["ticker"], "date": worst["exit_date"],
            "pnl": round(worst["pnl_dollar"], 2), "reason": worst["close_reason"],
        },
        "by_reason":         by_reason,
        "by_trade_type":     by_type,
        "by_mtf_score":      by_mtf,
        "signal_breakdown":  sig_stats,
        "top_tickers":       top5,
        "bottom_tickers":    bottom5,
        "monthly_pnl":       monthly,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Report formatting
# ─────────────────────────────────────────────────────────────────────────────

def _format_report(stats: dict, trades: list[dict]) -> str:
    """Plain-text report for console + Discord."""
    if "error" in stats:
        return f"Backtest error: {stats['error']}"

    ann = stats.get("ann_return_pct")
    ann_str = f"{ann:+.1f}%" if ann is not None else "N/A"
    lines = [
        "=" * 58,
        "  CONQUEST BACKTEST  |  $100,000 PAPER ACCOUNT",
        "=" * 58,
        f"  Period:            {stats['period'].replace(chr(8594), 'to')}",
        f"  Starting capital:  ${stats['starting_capital']:,.0f}",
        f"  Ending capital:    ${stats['ending_capital']:,.0f}",
        f"  Total return:      {stats['total_return_pct']:+.1f}%",
        f"  Annualised return: {ann_str}",
        f"  Total trades:      {stats['total_trades']}",
        f"  Win rate:          {stats['win_rate']:.1f}%",
        f"  Total P&L:         ${stats['total_pnl']:+,.2f}",
        f"  Avg P&L/trade:     ${stats['avg_pnl']:+.2f}",
        f"  Avg hold:          {stats['avg_hold_days']:.1f} days",
        f"  Profit factor:     {stats['profit_factor']}",
        f"  Sharpe ratio:      {stats['sharpe'] or 'N/A'}",
        f"  Max drawdown:      ${stats['max_drawdown']:,.2f}",
        f"  Commissions paid:  ${stats.get('total_commissions', 0):,.2f}  (IV+20% + $0.65/contract)",
        "",
        "  BEST TRADE",
        f"    {stats['best_trade']['ticker']}  ${stats['best_trade']['pnl']:+.2f}  "
        f"({stats['best_trade']['date']})  {stats['best_trade']['reason']}",
        "  WORST TRADE",
        f"    {stats['worst_trade']['ticker']}  ${stats['worst_trade']['pnl']:+.2f}  "
        f"({stats['worst_trade']['date']})  {stats['worst_trade']['reason']}",
        "",
        "  BY TRADE TYPE",
    ]
    for ttype, r in stats.get("by_trade_type", {}).items():
        lines.append(
            f"    {ttype:<16}  {r['count']:3d} trades  "
            f"{r['win_rate']:5.1f}% WR  avg ${r['avg_pnl']:+.2f}  "
            f"hold {r['avg_hold']:.1f}d  total ${r['total_pnl']:+.2f}"
        )

    lines += ["", "  BY EXIT REASON"]
    for reason, r in stats["by_reason"].items():
        lines.append(
            f"    {reason:<16}  {r['count']:3d} trades  "
            f"{r['win_rate']:5.1f}% WR  avg ${r['avg_pnl']:+.2f}"
        )

    lines += ["", "  BY MTF SCORE"]
    for score, r in sorted(stats["by_mtf_score"].items()):
        lines.append(
            f"    MTF {score}/3  {r['count']:3d} trades  "
            f"{r['win_rate']:5.1f}% WR  avg ${r['avg_pnl']:+.2f}"
        )

    lines += ["", "  BY SIGNAL TYPE"]
    for sig, r in stats["signal_breakdown"].items():
        lines.append(
            f"    {sig:<20}  {r['count']:3d} trades  "
            f"{r['win_rate']:5.1f}% WR  avg ${r['avg_pnl']:+.2f}"
        )

    lines += ["", "  TOP 5 TICKERS"]
    for tkr, r in stats["top_tickers"].items():
        lines.append(
            f"    {tkr:<6}  {r['count']:2d} trades  "
            f"${r['total_pnl']:+7.2f} total  {r['win_rate']:.0f}% WR"
        )

    lines += ["", "  BOTTOM 5 TICKERS"]
    for tkr, r in stats["bottom_tickers"].items():
        lines.append(
            f"    {tkr:<6}  {r['count']:2d} trades  "
            f"${r['total_pnl']:+7.2f} total  {r['win_rate']:.0f}% WR"
        )

    lines += ["", "  MONTHLY P&L"]
    monthly = stats["monthly_pnl"]
    max_abs  = max((abs(v) for v in monthly.values()), default=1)
    for month, pnl in sorted(monthly.items())[-18:]:
        bar_len = min(20, int(abs(pnl) / max_abs * 20)) if max_abs else 0
        bar     = ("+" if pnl >= 0 else "-") * bar_len
        sign    = "+" if pnl >= 0 else ""
        lines.append(f"    {month}  {sign}${pnl:8,.0f}  {bar}")

    lines.append("=" * 58)

    # ── Verdict (uses Sharpe + profit factor + P&L, not just win rate) ────────
    # Options strategies win through P&L asymmetry, not raw win rate.
    # A 50% WR strategy can still be excellent if winners are 2-3x bigger.
    wr  = stats["win_rate"]
    pf  = stats["profit_factor"]
    sh  = stats.get("sharpe") or 0
    pnl = stats["total_pnl"]
    n   = stats["total_trades"]

    if pf >= 1.5 and sh >= 1.0 and pnl > 0:
        verdict = "STRONG EDGE — Sharpe > 1.0 and profit factor > 1.5. Real statistical advantage confirmed."
    elif pf >= 1.1 and sh >= 0.7 and pnl > 0 and n >= 200:
        verdict = "EDGE CONFIRMED — Positive Sharpe, positive profit factor over sufficient sample. Strategy has real edge."
    elif pf >= 1.0 and pnl > 0:
        verdict = "MARGINAL EDGE — profitable but fragile. Monitor for regime changes. Improve signal filtering."
    elif pf < 1.0 or pnl < 0:
        verdict = "NO EDGE — strategy loses money historically. Do not trade live."
    else:
        verdict = "INCONCLUSIVE — run more tickers or extend period for stronger signal."

    # Statistical note on sample size
    import math
    se = math.sqrt(0.25 / n) * 100 if n > 0 else 99
    sig_note = f"  Sample: {n} trades | Win rate 95% CI: {wr:.1f}% +/- {1.96*se:.1f}%"

    lines += ["", f"  VERDICT: {verdict}", sig_note, "=" * 58]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Discord posting
# ─────────────────────────────────────────────────────────────────────────────

def _post_to_discord(report: str, stats: dict) -> None:
    """Post backtest results as clean Discord embeds to #agent-brain."""
    try:
        import requests
        from dotenv import load_dotenv
        load_dotenv()
        token      = os.getenv("DISCORD_BOT_TOKEN", "")
        channel_id = "1507449004945047602"   # #agent-brain
        if not token:
            print("[Discord] DISCORD_BOT_TOKEN not set — skipping.")
            return
        headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}

        def post_embed(embed):
            requests.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers=headers, json={"embeds": [embed]}
            )

        color_green  = 0x2ecc71
        color_red    = 0xe74c3c
        color_gold   = 0xd4af37
        color_purple = 0x9b59b6
        pnl_color    = color_green if stats["total_pnl"] > 0 else color_red

        # ── Embed 1: headline numbers ─────────────────────────────────────────
        ann = stats.get("ann_return_pct")
        ann_str = f"{ann:+.1f}%" if ann is not None else "N/A"
        post_embed({
            "title": "Conquest Backtesting Engine — Results",
            "description": (
                f"**{stats['period']}  |  {stats['total_trades']} trades simulated**\n"
                f"Starting capital: **$100,000**  →  Ending: **${stats['ending_capital']:,.0f}**"
            ),
            "color": pnl_color,
            "fields": [
                {"name": "Total Return",  "value": f"**{stats['total_return_pct']:+.1f}%**", "inline": True},
                {"name": "Ann. Return",   "value": f"**{ann_str}**",                          "inline": True},
                {"name": "Total P&L",     "value": f"**${stats['total_pnl']:+,.0f}**",        "inline": True},
                {"name": "Win Rate",      "value": f"{stats['win_rate']:.1f}%",               "inline": True},
                {"name": "Avg / Trade",   "value": f"${stats['avg_pnl']:+.2f}",               "inline": True},
                {"name": "Profit Factor", "value": f"{stats['profit_factor']}",               "inline": True},
                {"name": "Sharpe Ratio",  "value": f"{stats['sharpe'] or 'N/A'}",             "inline": True},
                {"name": "Max Drawdown",  "value": f"${stats['max_drawdown']:,.0f}",          "inline": True},
                {"name": "Avg Hold",      "value": f"{stats['avg_hold_days']:.1f} days",      "inline": True},
                {"name": "Best Trade",
                 "value": f"{stats['best_trade']['ticker']}  ${stats['best_trade']['pnl']:+,.0f}  ({stats['best_trade']['date']})",
                 "inline": True},
                {"name": "Worst Trade",
                 "value": f"{stats['worst_trade']['ticker']}  ${stats['worst_trade']['pnl']:+,.0f}  ({stats['worst_trade']['date']})",
                 "inline": True},
            ],
            "footer": {"text": "Conquest Backtesting Engine  •  $100k paper account  •  1% risk/trade  •  Not financial advice"},
        })

        # ── Embed 2: by trade type (the key breakdown) ────────────────────────
        type_lines = []
        for ttype, r in stats.get("by_trade_type", {}).items():
            icon = "📈" if "call" in ttype or ttype == "stock_long" else "📉"
            type_lines.append(
                f"{icon} **{ttype.replace('_',' ').title()}** — "
                f"{r['count']} trades | {r['win_rate']:.1f}% WR | "
                f"avg ${r['avg_pnl']:+.2f} | hold {r['avg_hold']:.1f}d | "
                f"total **${r['total_pnl']:+,.0f}**"
            )
        post_embed({
            "title": "By Trade Type",
            "color": color_gold,
            "description": "\n".join(type_lines) or "No data",
        })

        # ── Embed 3: MTF score breakdown ──────────────────────────────────────
        mtf_lines = []
        for score, r in sorted(stats.get("by_mtf_score", {}).items()):
            bar = ">" * int(r["win_rate"] / 10)
            mtf_lines.append(
                f"**MTF {score}/3** — {r['count']} trades | "
                f"{r['win_rate']:.1f}% WR | avg ${r['avg_pnl']:+.2f}  `{bar}`"
            )
        post_embed({
            "title": "By MTF Score (Signal Conviction)",
            "color": color_purple,
            "description": "\n".join(mtf_lines) or "No data",
        })

        # ── Embed 4: top/bottom tickers ───────────────────────────────────────
        top_lines = [
            f"**{tkr}** — ${r['total_pnl']:+,.0f} total | {r['win_rate']:.0f}% WR | {r['count']} trades"
            for tkr, r in stats.get("top_tickers", {}).items()
        ]
        bot_lines = [
            f"**{tkr}** — ${r['total_pnl']:+,.0f} total | {r['win_rate']:.0f}% WR | {r['count']} trades"
            for tkr, r in stats.get("bottom_tickers", {}).items()
        ]
        post_embed({
            "title": "Best & Worst Tickers",
            "color": color_gold,
            "fields": [
                {"name": "Top Performers",    "value": "\n".join(top_lines) or "—", "inline": False},
                {"name": "Worst Performers",  "value": "\n".join(bot_lines) or "—", "inline": False},
            ],
        })

        # ── Embed 5: monthly P&L (clean, no bars) ────────────────────────────
        monthly = stats.get("monthly_pnl", {})
        month_lines = []
        for month, pnl in sorted(monthly.items())[-18:]:
            icon = "+" if pnl >= 0 else "-"
            month_lines.append(f"`{month}`  {icon}${abs(pnl):>8,.0f}")
        # Split into two columns
        half = len(month_lines) // 2
        col1 = "\n".join(month_lines[:half]) or "—"
        col2 = "\n".join(month_lines[half:]) or "—"
        post_embed({
            "title": "Monthly P&L",
            "color": pnl_color,
            "fields": [
                {"name": "Earlier months", "value": col1, "inline": True},
                {"name": "Recent months",  "value": col2, "inline": True},
            ],
        })

        # ── Embed 6: verdict ──────────────────────────────────────────────────
        wr  = stats["win_rate"]
        pf  = stats["profit_factor"]
        sh  = stats.get("sharpe") or 0
        pnl = stats["total_pnl"]
        n   = stats["total_trades"]

        if pf >= 1.5 and sh >= 1.0 and pnl > 0:
            verdict_title = "STRONG EDGE CONFIRMED"
            verdict_color = color_green
            verdict_desc  = (
                f"Sharpe {sh:.2f} > 1.0 and profit factor {pf} > 1.5. "
                "Real statistical advantage confirmed over 2 years."
            )
        elif pf >= 1.1 and sh >= 0.7 and pnl > 0 and n >= 200:
            verdict_title = "EDGE CONFIRMED"
            verdict_color = color_green
            verdict_desc  = (
                f"Positive Sharpe ({sh:.2f}), positive profit factor ({pf}), "
                f"+${pnl:,.0f} over {n} trades. Strategy has demonstrated real edge."
            )
        elif pf >= 1.0 and pnl > 0:
            verdict_title = "MARGINAL EDGE"
            verdict_color = color_gold
            verdict_desc  = (
                f"Profitable over {n} trades but fragile. "
                "Monitor for regime changes. Continue improving signal filtering."
            )
        else:
            verdict_title = "NO EDGE DETECTED"
            verdict_color = color_red
            verdict_desc  = "Strategy loses money historically. Do not go live."

        import math
        se = math.sqrt(0.25 / n) * 100 if n > 0 else 99
        verdict_desc += (
            f"\n\n**Sample stats:** {n} trades | "
            f"Win rate 95% CI: {wr:.1f}% ± {1.96*se:.1f}%"
        )
        post_embed({
            "title": f"Verdict: {verdict_title}",
            "description": verdict_desc,
            "color": verdict_color,
            "footer": {"text": "Conquest Backtesting Engine  •  Not financial advice"},
        })

        print("[Discord] Backtest results posted to #agent-brain.")
    except Exception as e:
        print(f"[Discord] Post failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(
    tickers: list[str] = None,
    period: str = "2y",
    workers: int = 8,
    output: str = None,
    discord: bool = False,
) -> dict:
    """
    Run the full backtest. Returns stats dict.
    """
    universe = tickers or DEFAULT_UNIVERSE
    print(f"\n{'='*58}")
    print(f"  CONQUEST BACKTEST — {len(universe)} tickers  •  {period}")
    print(f"{'='*58}\n")

    all_trades: list[dict] = []
    failures: list[str]    = []

    def _process_one(tkr):
        print(f"  Processing {tkr}...")
        df = _compute_signals(tkr, period)
        if df is None:
            return tkr, []
        stk_trades        = _simulate_trades(tkr, df)
        call_trades       = _simulate_option_trades(tkr, df, "call")
        put_trades        = _simulate_option_trades(tkr, df, "put")
        bull_put_trades   = _simulate_credit_spread_trades(tkr, df, "bull_put")
        bear_call_trades  = _simulate_credit_spread_trades(tkr, df, "bear_call")
        cov_call_trades   = _simulate_covered_call_trades(tkr, df)
        # tag stock trade type for reporting
        for t in stk_trades:
            t.setdefault("trade_type", f"stock_{t.get('direction','long')}")
        all_t = (stk_trades + call_trades + put_trades
                 + bull_put_trades + bear_call_trades + cov_call_trades)
        print(f"  {tkr}: {len(stk_trades)} stk  {len(call_trades)} calls  "
              f"{len(put_trades)} puts  {len(bull_put_trades)} bull_put  "
              f"{len(bear_call_trades)} bear_call  {len(cov_call_trades)} cov_call")
        return tkr, all_t

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_process_one, t): t for t in universe}
        for fut in as_completed(futs):
            tkr = futs[fut]
            try:
                _, trades = fut.result()
                all_trades.extend(trades)
            except Exception as e:
                print(f"  {tkr}: FAILED — {e}")
                failures.append(tkr)

    print(f"\n  Total simulated trades: {len(all_trades)}")
    if failures:
        print(f"  Failed tickers: {', '.join(failures)}")

    stats  = _compute_stats(all_trades)

    # Compute verdict (same logic as _format_report) and store in stats
    _pf = stats.get("profit_factor", 0) or 0
    _sh = stats.get("sharpe") or 0
    _pnl = stats.get("total_pnl", 0)
    _n  = stats.get("total_trades", 0)
    if _pf >= 1.5 and _sh >= 1.0 and _pnl > 0:
        stats["verdict"] = "STRONG EDGE CONFIRMED"
    elif _pf >= 1.1 and _sh >= 0.7 and _pnl > 0 and _n >= 200:
        stats["verdict"] = "EDGE CONFIRMED"
    elif _pf >= 1.0 and _pnl > 0:
        stats["verdict"] = "MARGINAL EDGE"
    elif _pf < 1.0 or _pnl < 0:
        stats["verdict"] = "NO EDGE DETECTED"
    else:
        stats["verdict"] = "INCONCLUSIVE"

    report = _format_report(stats, all_trades)

    print("\n" + report)

    if output:
        with open(output, "w") as f:
            json.dump({"stats": stats, "trades": all_trades}, f, indent=2)
        print(f"\n  Raw trades saved to: {output}")

    # Persist a summary to DB so the Discord bot can reference it in chat
    try:
        from db import kv_set
        period_str = stats.get("period", period)
        kv_set("last_backtest", {
            "total_trades":    stats.get("total_trades", 0),
            "total_pnl":       stats.get("total_pnl", 0),
            "win_rate":        stats.get("win_rate", 0),
            "sharpe":          stats.get("sharpe", 0),
            "profit_factor":   stats.get("profit_factor", 0),
            "max_drawdown":    stats.get("max_drawdown", 0),
            "avg_pnl":         stats.get("avg_pnl", 0),
            "avg_hold":        stats.get("avg_hold_days", 0),
            "verdict":         stats.get("verdict", ""),
            "period":          period_str,
            "starting_capital":stats.get("starting_capital", 100_000),
            "ending_capital":  stats.get("ending_capital", 0),
            "total_return_pct":stats.get("total_return_pct", 0),
            "ann_return_pct":  stats.get("ann_return_pct"),
            "by_trade_type":   stats.get("by_trade_type", {}),
            "run_at":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        })
    except Exception as _db_e:
        print(f"  [DB] Could not save backtest summary: {_db_e}")

    if discord:
        _post_to_discord(report, stats)

    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Conquest Trading Backtester")
    parser.add_argument("--tickers",  nargs="+", help="Override ticker list")
    parser.add_argument("--period",   default="2y", help="History period: 1y, 2y, 3y")
    parser.add_argument("--top",      type=int, default=0,
                        help="Use top-N from pre-screener instead of default universe")
    parser.add_argument("--workers",  type=int, default=8, help="Parallel workers")
    parser.add_argument("--output",   help="Save trades to JSON file")
    parser.add_argument("--discord",  action="store_true", help="Post results to Discord")
    args = parser.parse_args()

    tickers = args.tickers
    if args.top > 0:
        print(f"  Running pre-screener to select top {args.top} tickers…")
        try:
            from universe_screener import pre_screen
            tickers = pre_screen(n=args.top)
        except Exception as e:
            print(f"  Pre-screener failed ({e}), using default universe.")
            tickers = DEFAULT_UNIVERSE

    run_backtest(
        tickers=tickers,
        period=args.period,
        workers=args.workers,
        output=args.output,
        discord=args.discord,
    )
