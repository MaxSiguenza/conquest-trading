# -*- coding: utf-8 -*-
"""
Multi-Timeframe Dashboard
Run: python mtf_dashboard.py AAPL NVDA ...
"""
import sys
sys.path.insert(0, ".")

from datetime import date
import pandas as pd

from config import Config, DataConfig
from data.fetcher import fetch_ohlcv, fetch_vix, get_earnings_dates
from signals.generator import generate_signals
from indicators.volatility import calculate_hv_rank, spread_recommendation
from macro.fetcher import fetch_macro_data, macro_health_score, sector_rotation_phase, stock_macro_warnings

DEFAULT_WATCHLIST = ["AAPL", "NVDA", "MSFT", "TSLA", "PLTR", "SPY", "QQQ", "AMZN", "META", "GOOGL"]

# Sector ETF for each ticker — used to confirm the whole sector is bullish
SECTOR_ETF = {
    "COP": "XLE",  "EOG": "XLE",  "XOM": "XLE",  "CVX": "XLE",  "SLB": "XLE",
    "AAPL":"XLK",  "MSFT":"XLK",  "NVDA":"XLK",  "AVGO":"XLK",  "AMD": "XLK",
    "PLTR":"XLK",  "TSLA":"XLK",  "AMZN":"XLK",
    "WMT": "XLP",  "KO":  "XLP",  "PG":  "XLP",  "COST":"XLP",
    "FCX": "XLB",  "LIN": "XLB",  "NEM": "XLB",
    "CAT": "XLI",  "DE":  "XLI",  "HON": "XLI",
    "GOOGL":"XLC", "META":"XLC",  "NFLX":"XLC",
    "UNH": "XLV",  "JNJ": "XLV",  "PFE": "XLV",
    "PLD": "XLRE", "AMT": "XLRE",
    "JPM": "XLF",  "BAC": "XLF",  "GS":  "XLF",
    "SPY": "SPY",  "QQQ": "QQQ",
}

if len(sys.argv) > 1:
    WATCHLIST = [t.upper() for t in sys.argv[1:]]
else:
    user_input = input("Enter tickers (or press Enter for default): ").strip()
    WATCHLIST = [t.upper() for t in user_input.split()] if user_input else DEFAULT_WATCHLIST


def regime_label(val) -> str:
    return "BULL" if val == 1 else "BEAR"

def score_label(score: int) -> str:
    if score == 3:   return "STRONG BUY  [3/3]"
    elif score == 2: return "BUY         [2/3]"
    elif score == 1: return "WEAK        [1/3]"
    else:            return "STAY OUT    [0/3]"


print(f"\n{'='*105}")
print(f"  MULTI-TIMEFRAME SIGNAL DASHBOARD  --  {date.today()}")
print(f"{'='*105}")

# Macro health header — quick 1-line summary before the main table
try:
    macro_data = fetch_macro_data()
    m_score, m_max = macro_health_score(macro_data)
    phase, _, _ = sector_rotation_phase(macro_data)
    m_bar  = "#" * m_score + "-" * (m_max - m_score)
    m_grade = "FAVORABLE" if m_score >= 4 else "NEUTRAL" if m_score >= 2 else "CAUTION"
    print(f"  MACRO: {m_score}/{m_max} [{m_bar}] {m_grade}  |  Phase: {phase}")
    print(f"  Run 'python macro/dashboard.py' for full macro breakdown")
except Exception:
    print(f"  MACRO: unavailable (run 'python macro/dashboard.py' separately)")
print(f"  {'-'*103}")

cfg_base = Config()
vix_series = fetch_vix(cfg_base.data.start_date, cfg_base.data.end_date)

# Cache sector ETF signals so we only fetch each ETF once
etf_cache = {}

def get_etf_regime(etf: str) -> int:
    if etf in etf_cache:
        return etf_cache[etf]
    try:
        cfg = Config(data=DataConfig(ticker=etf))
        df  = fetch_ohlcv(etf, cfg.data.start_date, cfg.data.end_date)
        df  = generate_signals(df, cfg.indicators, vix=vix_series)
        regime = int(df.iloc[-1].get("Regime", 0))
        etf_cache[etf] = regime
        return regime
    except Exception:
        etf_cache[etf] = -1
        return -1


# Collect all data first for RS ranking
rows = []

for ticker in WATCHLIST:
    try:
        cfg = Config(data=DataConfig(ticker=ticker))
        df  = fetch_ohlcv(ticker, cfg.data.start_date, cfg.data.end_date)
        earnings = get_earnings_dates(ticker)
        df  = generate_signals(df, cfg.indicators, vix=vix_series, earnings_dates=earnings)

        last    = df.iloc[-1]
        price   = float(last["Close"])
        daily   = int(last.get("Regime", 0))
        weekly  = int(last.get("W_Regime", 0))
        monthly = int(last.get("M_Regime", 0))
        score   = int(last.get("MTF_Score", 0))
        entry   = int(last.get("Entry_Signal", 0))
        vol_ok  = bool(last.get("Vol_OK", True))

        adx_val   = float(last["ADX"])   if "ADX"        in last and not pd.isna(last["ADX"])        else 0.0
        squeeze   = bool(last["BB_Squeeze"]) if "BB_Squeeze"  in last and not pd.isna(last["BB_Squeeze"]) else False
        obv_trend = bool(last["OBV_Trend"])  if "OBV_Trend"   in last and not pd.isna(last["OBV_Trend"])  else True
        near_high = bool(last["Near_52w_High"]) if "Near_52w_High" in last else False

        hvr       = calculate_hv_rank(df)
        hv_rank   = hvr["hv_rank"]

        etf       = SECTOR_ETF.get(ticker, "")
        etf_regime = get_etf_regime(etf) if etf else -1

        rs_return = 0.0
        if len(df) >= 63:
            rs_return = (df["Close"].iloc[-1] - df["Close"].iloc[-63]) / df["Close"].iloc[-63]

        rows.append({
            "ticker": ticker, "price": price,
            "monthly": monthly, "weekly": weekly, "daily": daily,
            "score": score, "entry": entry, "vol_ok": vol_ok,
            "adx": adx_val, "squeeze": squeeze, "obv_trend": obv_trend,
            "near_high": near_high, "hv_rank": hv_rank,
            "etf": etf, "etf_regime": etf_regime,
            "rs_return": rs_return, "error": None,
        })
    except Exception as e:
        rows.append({"ticker": ticker, "error": str(e)})

# RS rank within this group
valid_rows = [r for r in rows if not r.get("error")]
for rank, r in enumerate(sorted(valid_rows, key=lambda x: x["rs_return"], reverse=True), 1):
    r["rs_rank"] = rank

# Main table
print(f"  {'Ticker':<7} {'Price':>8}  {'Mon':>5} {'Wk':>5} {'Day':>5}  "
      f"{'Sc':>4}  {'ADX':>5}  {'HVR':>4}  {'RS#':>4}  {'Sqz':>4}  {'OBV':>4}  {'ETF':>5}  Signal")
print(f"  {'-'*98}")

for r in rows:
    ticker = r["ticker"]
    if r.get("error"):
        print(f"  {ticker:<7}  ERROR: {r['error']}")
        continue

    entry_tag  = "  <-- ENTER NOW" if r["entry"] == 1 else ""
    sqz_tag    = "YES" if r["squeeze"]   else " no"
    obv_tag    = " OK" if r["obv_trend"] else " --"
    vol_tag    = " OK" if r["vol_ok"]    else " --"
    adx_str    = f"{r['adx']:.0f}"
    etf_str    = "BULL" if r["etf_regime"] == 1 else ("BEAR" if r["etf_regime"] == 0 else "  --")

    print(
        f"  {ticker:<7} ${r['price']:>7.2f}  "
        f"{regime_label(r['monthly']):>5} "
        f"{regime_label(r['weekly']):>5} "
        f"{regime_label(r['daily']):>5}  "
        f"{r['score']:>2}/3  "
        f"{adx_str:>5}  "
        f"{r['hv_rank']:>4.0f}  "
        f"#{r.get('rs_rank', '-'):>3}  "
        f"{sqz_tag:>4}  "
        f"{obv_tag:>4}  "
        f"{etf_str:>5}  "
        f"{score_label(r['score'])}"
        f"{entry_tag}"
    )

# Spread type section
bullish = [r for r in valid_rows if r["score"] >= 2]
if bullish:
    print(f"\n  {'-'*98}")
    print(f"  SPREAD RECOMMENDATION  (HVR-driven):")
    macro_warns = {}
    try:
        macro_warns = stock_macro_warnings([r["ticker"] for r in bullish], macro_data)
    except Exception:
        pass
    for r in bullish:
        spread_type, reason = spread_recommendation(r["hv_rank"], r["score"])
        etf_warn     = f"  [!] Sector ETF {r['etf']} BEARISH" if r["etf_regime"] == 0 else ""
        squeeze_note = "  [SQUEEZE]" if r["squeeze"] else ""
        macro_flag   = f"  [MACRO] {macro_warns[r['ticker']][0]}" if macro_warns.get(r["ticker"]) else ""
        print(f"  {r['ticker']:<7}  {spread_type:<25}  {reason}{etf_warn}{squeeze_note}{macro_flag}")

# RS ranking
print(f"\n  {'-'*98}")
print(f"  RELATIVE STRENGTH (3-month return):")
for r in sorted(valid_rows, key=lambda x: x.get("rs_rank", 99)):
    sign = "+" if r["rs_return"] >= 0 else ""
    near = "  [near 52w high]" if r["near_high"] else ""
    print(f"  #{r.get('rs_rank','?'):<2}  {r['ticker']:<7}  {sign}{r['rs_return']:.1%}{near}")

# VIX + legend
vix_level = float(vix_series.iloc[-1]) if not vix_series.empty else 0
print(f"\n  VIX: {vix_level:.1f}  --  ", end="")
if vix_level < 20:   print("Market calm. All signals valid.")
elif vix_level < 30: print("Market elevated. Reduce position size.")
else:                print("Market panic! No new entries.")

print(f"""
  HOW TO READ THIS:
  Mon/Wk/Day  = regime at each timeframe (BULL = golden cross active)
  Sc          = how many timeframes agree (3/3 = highest conviction)
  ADX         = trend strength. >25 strong, <20 skip or reduce size
  HVR         = vol rank 0-100. >50 sell premium, <30 buy options
  RS#         = relative strength rank in this scan (#1 = strongest)
  Sqz         = Bollinger Band squeeze (YES = coiling for breakout)
  OBV         = OK = institutions accumulating, -- = distribution
  ETF         = sector ETF regime (BULL confirms the whole sector is healthy)
  ENTER NOW   = all filters passed: MTF, MACD, RSI, VIX, volume, OBV, ADX
{'='*105}
""")
