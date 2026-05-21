# -*- coding: utf-8 -*-
"""
FRED Macro Data Module
======================
Pulls Federal Reserve economic data (FRED) for the macro dashboard.
Provides GDP, CPI, Fed Funds, yield curve, unemployment, and consumer sentiment.

Requires: fredapi  (pip install fredapi)
          FRED_API_KEY in .env file  (free key at fred.stlouisfed.org/docs/api/api_key.html)
"""
import os

_ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

# Series to fetch — ID : display metadata
FRED_SERIES = {
    "GDPC1":    {"name": "Real GDP (Quarterly)", "unit": "$B",  "desc": "Inflation-adjusted US economic output"},
    "CPIAUCSL": {"name": "CPI (All Items)",       "unit": "idx", "desc": "Consumer Price Index — key inflation gauge"},
    "FEDFUNDS": {"name": "Fed Funds Rate",        "unit": "%",   "desc": "Federal Reserve target interest rate"},
    "DGS10":    {"name": "10Y Treasury (FRED)",   "unit": "%",   "desc": "Daily 10-year yield direct from Fed"},
    "T10Y2Y":   {"name": "Yield Curve (10Y-2Y)", "unit": "%",   "desc": "+ = normal, - = INVERTED (recession signal)"},
    "UNRATE":   {"name": "Unemployment Rate",     "unit": "%",   "desc": "US unemployment — labor market health"},
    "UMCSENT":  {"name": "Consumer Sentiment",    "unit": "idx", "desc": "U. of Michigan consumer confidence index"},
}


def _get_fred():
    """Return an authenticated Fred client."""
    from fredapi import Fred
    from dotenv import dotenv_values
    vals = dotenv_values(_ENV_FILE)
    key  = vals.get("FRED_API_KEY") or os.getenv("FRED_API_KEY", "")
    if not key:
        raise RuntimeError("FRED_API_KEY not found in .env — add it to enable FRED macro data.")
    return Fred(api_key=key)


def fetch_fred_macro() -> dict:
    """
    Fetch the latest reading for each FRED series.
    Returns a dict keyed by series ID.
    Each entry contains: name, unit, desc, latest, prev, change, yoy, qoq, as_of, error
    """
    try:
        fred = _get_fred()
    except Exception as e:
        # Return error stubs for all series so the dashboard can still render
        return {
            sid: {"name": meta["name"], "unit": meta["unit"],
                  "desc": meta["desc"], "latest": None, "error": str(e)}
            for sid, meta in FRED_SERIES.items()
        }

    results = {}
    for series_id, meta in FRED_SERIES.items():
        try:
            s      = fred.get_series(series_id, observation_start="2010-01-01")
            s      = s.dropna()
            if s.empty:
                raise ValueError("No data returned")

            latest = float(s.iloc[-1])
            prev   = float(s.iloc[-2]) if len(s) > 1 else latest
            change = latest - prev

            # Year-over-year for CPI (12 monthly observations back)
            yoy = None
            if series_id == "CPIAUCSL" and len(s) >= 13:
                yoy = round((latest / float(s.iloc[-13]) - 1) * 100, 2)

            # Quarter-over-quarter annualized for GDP
            qoq = None
            if series_id == "GDPC1" and len(s) >= 2:
                qoq = round(((latest / float(s.iloc[-2])) ** 4 - 1) * 100, 2)

            results[series_id] = {
                "name":   meta["name"],
                "unit":   meta["unit"],
                "desc":   meta["desc"],
                "latest": round(latest, 3),
                "prev":   round(prev,   3),
                "change": round(change, 3),
                "yoy":    yoy,
                "qoq":    qoq,
                "as_of":  str(s.index[-1].date()),
                "error":  None,
            }
        except Exception as e:
            results[series_id] = {
                "name":   meta["name"],
                "unit":   meta["unit"],
                "desc":   meta["desc"],
                "latest": None,
                "error":  str(e),
            }

    return results


def fred_macro_context(data: dict) -> str:
    """
    Return a compact one-line string of FRED data for use in Claude prompts.
    Example: "GDP: $29,842B (+2.8% ann.) | CPI: 314.9 (+3.2% YoY) | Fed Funds: 4.33% | ..."
    """
    parts = []
    for sid, r in data.items():
        if r.get("error") or r.get("latest") is None:
            continue
        val = r["latest"]

        if sid == "GDPC1":
            qoq = r.get("qoq")
            parts.append(f"GDP: ${val:,.0f}B" + (f" ({qoq:+.1f}% ann.)" if qoq is not None else ""))
        elif sid == "CPIAUCSL":
            yoy = r.get("yoy")
            parts.append(f"CPI: {val:.1f}" + (f" ({yoy:+.1f}% YoY)" if yoy is not None else ""))
        elif sid == "FEDFUNDS":
            parts.append(f"Fed Funds: {val:.2f}%")
        elif sid == "DGS10":
            parts.append(f"10Y: {val:.2f}%")
        elif sid == "T10Y2Y":
            status = "normal" if val >= 0 else "INVERTED"
            parts.append(f"Yield Curve: {val:+.2f}% ({status})")
        elif sid == "UNRATE":
            parts.append(f"Unemployment: {val:.1f}%")
        elif sid == "UMCSENT":
            parts.append(f"Sentiment: {val:.1f}")

    return " | ".join(parts) if parts else "FRED data unavailable"
