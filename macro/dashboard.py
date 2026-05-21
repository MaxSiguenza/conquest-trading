# -*- coding: utf-8 -*-
"""
Macro Dashboard
================
Shows the full economic backdrop behind your trades.
Run before making any options or position decisions.

Usage:
  python macro/dashboard.py
  python macro/dashboard.py COP EOG WMT    -- include stock-specific warnings
"""
import sys
sys.path.insert(0, ".")

from datetime import date
from macro.fetcher import fetch_macro_data, macro_health_score, sector_rotation_phase, stock_macro_warnings


def run(tickers: list = None):
    print(f"\n{'='*72}")
    print(f"  MACRO HEALTH DASHBOARD  --  {date.today()}")
    print(f"{'='*72}")
    print(f"  Fetching macro data...")

    data  = fetch_macro_data()
    score, max_score = macro_health_score(data)
    phase, phase_desc, best_sectors = sector_rotation_phase(data)

    # Health score bar
    bar   = "#" * score + "-" * (max_score - score)
    grade = "FAVORABLE" if score >= 4 else "NEUTRAL" if score >= 2 else "CAUTION"
    print(f"\n  MACRO HEALTH SCORE: {score}/{max_score}  [{bar}]  {grade}")

    # Main table
    print(f"\n  {'Indicator':<22} {'Value':>8}  {'Trend':>8}  {'Regime':>8}  {'52w Range':>10}  Note")
    print(f"  {'-'*80}")

    display_order = ["HYG", "DX-Y.NYB", "^TNX", "YIELD_CURVE", "CL=F", "HG=F", "SPY"]

    for key in display_order:
        r = data.get(key)
        if not r or r.get("error") and not r.get("current"):
            continue

        name    = r["name"]
        current = r.get("current")
        regime  = r.get("regime", -1)
        trend   = r.get("trend", "")
        pct     = r.get("pct_range")

        if current is None:
            print(f"  {name:<22}  {'N/A':>8}   {'?':>8}   {'?':>8}")
            continue

        regime_str = "BULL" if regime == 1 else ("BEAR" if regime == 0 else " -- ")
        pct_str    = f"{pct:.0f}% range" if pct is not None else ""

        # What this means for stocks
        note = ""
        if key == "HYG":
            note = "Risk-ON" if regime == 1 else "RISK-OFF -- reduce exposure"
        elif key == "DX-Y.NYB":
            note = "Commodity tailwind" if regime == 0 else "Commodity HEADWIND"
        elif key == "^TNX":
            note = "Growth tailwind" if trend == "FALLING" else "Growth HEADWIND"
        elif key == "YIELD_CURVE":
            note = "No recession signal" if regime == 1 else "RECESSION WARNING"
        elif key == "CL=F":
            note = "Energy tailwind" if regime == 1 else "Energy HEADWIND"
        elif key == "HG=F":
            note = "Expansion signal" if regime == 1 else "Slowdown signal"
        elif key == "SPY":
            note = "Market uptrend" if regime == 1 else "Market downtrend"

        if key == "^TNX" or key == "^IRX":
            val_str = f"{current:.2f}%"
        elif key == "YIELD_CURVE":
            val_str = f"{current:+.2f}%"
        else:
            val_str = f"${current:.2f}"

        print(f"  {name:<22} {val_str:>8}  {trend:>8}  {regime_str:>8}  {pct_str:>10}  {note}")

    # Sector rotation phase
    print(f"\n  {'='*70}")
    print(f"  ECONOMIC CYCLE PHASE: {phase}")
    print(f"  {phase_desc}")
    print(f"\n  Best sectors right now:")
    for s in best_sectors:
        print(f"    -> {s}")

    # Stock-specific warnings
    if tickers:
        warnings = stock_macro_warnings(tickers, data)
        has_warnings = any(w for w in warnings.values())
        print(f"\n  {'='*70}")
        print(f"  STOCK-SPECIFIC MACRO FLAGS:")
        for ticker, w in warnings.items():
            if w:
                for flag in w:
                    print(f"  {ticker:<7}  [!] {flag}")
            else:
                print(f"  {ticker:<7}  [OK] No macro headwinds")

    # ── FRED Federal Reserve Data ──────────────────────────────────────
    try:
        from macro.fred_data import fetch_fred_macro
        print(f"\n  {'='*70}")
        print(f"  FEDERAL RESERVE DATA (FRED)  —  Real economic indicators:")
        print(f"  {'Indicator':<28} {'Value':>12}  {'Change':>9}  Note")
        print(f"  {'-'*72}")

        fred_data = fetch_fred_macro()
        any_data  = False

        for sid, r in fred_data.items():
            name = r.get("name", sid)

            if r.get("error"):
                if not any_data:
                    print(f"  {name:<28}  {'ERROR: ' + str(r['error'])[:40]}")
                continue
            if r.get("latest") is None:
                continue

            any_data = True
            val  = r["latest"]
            chg  = r.get("change", 0) or 0

            if sid == "GDPC1":
                qoq     = r.get("qoq") or 0
                val_str = f"${val:>10,.0f}B"
                chg_str = f"{qoq:+.1f}% ann."
                note    = "Expansion" if qoq >= 0 else "CONTRACTION"
            elif sid == "CPIAUCSL":
                yoy     = r.get("yoy") or 0
                val_str = f"{val:>12.1f}"
                chg_str = f"{yoy:+.1f}% YoY"
                note    = ("ELEVATED — Fed hawkish risk" if yoy > 4
                           else "Cooling" if yoy > 2.5
                           else "Below target — Fed may cut")
            elif sid in ("FEDFUNDS", "DGS10"):
                val_str = f"{val:>11.2f}%"
                chg_str = f"{chg:+.3f}%"
                note    = "Restrictive — headwind for growth" if val >= 4.5 else "Supportive"
            elif sid == "T10Y2Y":
                val_str = f"{val:>+11.2f}%"
                chg_str = f"{chg:+.3f}%"
                note    = "Normal curve" if val >= 0 else "INVERTED — recession risk"
            elif sid == "UNRATE":
                val_str = f"{val:>11.1f}%"
                chg_str = f"{chg:+.1f}%"
                note    = "Strong labor market" if val <= 4.5 else "Weakening"
            elif sid == "UMCSENT":
                val_str = f"{val:>12.1f}"
                chg_str = f"{chg:+.1f}"
                note    = "Optimistic" if val > 80 else ("Neutral" if val > 60 else "Pessimistic")
            else:
                val_str = f"{val:>12}"
                chg_str = f"{chg:+.3f}"
                note    = ""

            as_of = f"  [{r.get('as_of','')}]" if r.get("as_of") else ""
            print(f"  {name:<28} {val_str}  {chg_str:>9}  {note}{as_of}")

        if not any_data:
            print(f"  FRED key not set or API unreachable — add FRED_API_KEY to .env")
            print(f"  Free key: fred.stlouisfed.org/docs/api/api_key.html")

    except Exception as e:
        print(f"\n  FRED data unavailable: {e}")

    print(f"\n  {'='*70}")
    print(f"  HOW TO USE THIS:")
    print(f"  Score 5-6 = strong macro tailwind. Full size positions.")
    print(f"  Score 3-4 = mixed. Favor put credits (collect premium, lower risk).")
    print(f"  Score 1-2 = headwinds. Reduce position size by 50%.")
    print(f"  Score 0   = danger. No new positions. Protect what you have.")
    print(f"\n  Run this BEFORE mtf_dashboard.py.")
    print(f"  If macro is bearish, no technical signal is strong enough to override it.")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    tickers = [t.upper() for t in sys.argv[1:]] if len(sys.argv) > 1 else []
    run(tickers)
