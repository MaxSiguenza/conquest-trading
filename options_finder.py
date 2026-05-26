# -*- coding: utf-8 -*-
"""
Conquest Trading — Options Finder
==================================
Finds live options plays (spreads + single-leg) for a given ticker and budget.
Pulled from the real-time yfinance options chain.

Usage (standalone):
    python options_finder.py AAPL 500
    python options_finder.py NVDA 1000 calls
    python options_finder.py TSLA 750 spreads

Discord:
    !options AAPL 500
    !options NVDA 1000 calls
    !options SPY spreads
"""

import sys
import os
from datetime import date, datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mid(bid: float, ask: float, last: float = 0.0) -> float:
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 4)
    if last > 0:
        return round(last, 4)
    return 0.0


def _best_expiry(expirations: tuple, min_dte: int = 21, target_dte: int = 40) -> Optional[str]:
    """Pick the expiry closest to target_dte with at least min_dte days remaining."""
    today = date.today()
    valid = [
        e for e in expirations
        if (date.fromisoformat(e) - today).days >= min_dte
    ]
    if not valid:
        return None
    return min(valid, key=lambda e: abs((date.fromisoformat(e) - today).days - target_dte))


def _dte(expiry: str) -> int:
    return (date.fromisoformat(expiry) - date.today()).days


# ── Core chain fetcher ────────────────────────────────────────────────────────

def get_chain(ticker: str, expiry: str, opt_type: str) -> list[dict]:
    """Return all liquid contracts for one expiry + side as list of dicts."""
    import yfinance as yf
    tk = yf.Ticker(ticker)
    chain = tk.option_chain(expiry)
    df = chain.calls if opt_type == "call" else chain.puts
    if df is None or df.empty:
        return []

    rows = []
    for _, row in df.iterrows():
        bid  = float(row.get("bid")  or 0)
        ask  = float(row.get("ask")  or 0)
        last = float(row.get("lastPrice") or 0)
        m    = _mid(bid, ask, last)
        if m <= 0.05:
            continue
        vol  = int(v) if (v := row.get("volume"))  is not None and str(v) != "nan" else 0
        oi   = int(v) if (v := row.get("openInterest")) is not None and str(v) != "nan" else 0
        # Skip completely dead contracts
        if vol == 0 and oi == 0:
            continue
        # Skip contracts with a bid-ask spread wider than 60% of mid (unfillable)
        if bid > 0 and ask > 0 and (ask - bid) / m > 0.60:
            continue
        iv   = float(row.get("impliedVolatility") or 0)
        rows.append({
            "strike": float(row["strike"]),
            "bid":    round(bid,  2),
            "ask":    round(ask,  2),
            "mid":    m,
            "last":   round(last, 2),
            "iv":     round(iv,   4),
            "volume": vol,
            "oi":     oi,
            "symbol": str(row.get("contractSymbol") or ""),
        })
    return sorted(rows, key=lambda r: r["strike"])


# ── Spread finder ─────────────────────────────────────────────────────────────

def _liquid(leg: dict) -> bool:
    """Both OI and volume can't both be near zero — contract must be tradeable."""
    return leg["oi"] >= 50 or leg["volume"] >= 10


def find_call_spreads(ticker: str, price: float, chain: list[dict],
                      budget: float, expiry: str) -> list[dict]:
    """Bull call debit spreads within budget. Long ATM–slightly OTM, short 1-2 steps out."""
    results = []
    n = len(chain)
    for i, long_leg in enumerate(chain):
        lk = long_leg["strike"]
        # Only consider long legs up to 10% OTM
        if lk > price * 1.10:
            break
        if lk < price * 0.90:
            continue
        if not _liquid(long_leg):
            continue
        for j in range(i + 1, min(i + 6, n)):
            short_leg = chain[j]
            if not _liquid(short_leg):
                continue
            sk = short_leg["strike"]
            width  = sk - lk
            if width < 2.0:
                continue
            debit  = round(long_leg["ask"] - short_leg["bid"], 2)
            if debit <= 0.10:
                continue
            cost   = round(debit * 100, 2)
            if cost > budget:
                continue
            max_profit  = round((width - debit) * 100, 2)
            breakeven   = round(lk + debit, 2)
            rr          = round(max_profit / cost, 2) if cost > 0 else 0
            be_move_pct = round((breakeven - price) / price * 100, 1)
            results.append({
                "type":       "call_spread",
                "label":      f"${lk:.0f}c / ${sk:.0f}c",
                "long_strike":  lk,
                "short_strike": sk,
                "width":      width,
                "debit":      debit,
                "cost":       cost,
                "max_profit": max_profit,
                "breakeven":  breakeven,
                "be_move_pct": be_move_pct,
                "rr":         rr,
                "expiry":     expiry,
                "dte":        _dte(expiry),
                "long_iv":    long_leg["iv"],
                "short_iv":   short_leg["iv"],
                "min_oi":     min(long_leg["oi"],  short_leg["oi"]),
                "min_vol":    min(long_leg["volume"], short_leg["volume"]),
            })
    # Sort by risk/reward descending, filter sensible RR
    results = [r for r in results if r["rr"] >= 0.5]
    return sorted(results, key=lambda r: -r["rr"])[:5]


def find_put_spreads(ticker: str, price: float, chain: list[dict],
                     budget: float, expiry: str) -> list[dict]:
    """Bear put debit spreads within budget. Long ATM–slightly OTM, short 1-2 steps below."""
    results = []
    n = len(chain)
    for i, long_leg in enumerate(chain):
        lk = long_leg["strike"]
        if lk > price * 1.05:
            continue
        if lk < price * 0.90:
            break
        if not _liquid(long_leg):
            continue
        for j in range(i - 1, max(i - 6, -1), -1):
            short_leg = chain[j]
            if not _liquid(short_leg):
                continue
            sk = short_leg["strike"]
            width = lk - sk
            if width < 2.0:
                continue
            debit = round(long_leg["ask"] - short_leg["bid"], 2)
            if debit <= 0.10:
                continue
            cost  = round(debit * 100, 2)
            if cost > budget:
                continue
            max_profit  = round((width - debit) * 100, 2)
            breakeven   = round(lk - debit, 2)
            rr          = round(max_profit / cost, 2) if cost > 0 else 0
            be_move_pct = round((breakeven - price) / price * 100, 1)
            results.append({
                "type":       "put_spread",
                "label":      f"${lk:.0f}p / ${sk:.0f}p",
                "long_strike":  lk,
                "short_strike": sk,
                "width":      width,
                "debit":      debit,
                "cost":       cost,
                "max_profit": max_profit,
                "breakeven":  breakeven,
                "be_move_pct": be_move_pct,
                "rr":         rr,
                "expiry":     expiry,
                "dte":        _dte(expiry),
                "long_iv":    long_leg["iv"],
                "short_iv":   short_leg["iv"],
                "min_oi":     min(long_leg["oi"],  short_leg["oi"]),
                "min_vol":    min(long_leg["volume"], short_leg["volume"]),
            })
    results = [r for r in results if r["rr"] >= 0.5]
    return sorted(results, key=lambda r: -r["rr"])[:5]


def find_long_calls(price: float, chain: list[dict],
                    budget: float, expiry: str) -> list[dict]:
    """Single-leg long calls within budget, ATM to 10% OTM."""
    results = []
    for leg in chain:
        k   = leg["strike"]
        if k < price * 0.98 or k > price * 1.12:
            continue
        cost = round(leg["ask"] * 100, 2)
        if cost > budget:
            continue
        be  = round(k + leg["ask"], 2)
        be_pct = round((be - price) / price * 100, 1)
        results.append({
            "type":      "long_call",
            "label":     f"${k:.0f}c",
            "strike":    k,
            "ask":       leg["ask"],
            "mid":       leg["mid"],
            "cost":      cost,
            "breakeven": be,
            "be_move_pct": be_pct,
            "iv":        leg["iv"],
            "volume":    leg["volume"],
            "oi":        leg["oi"],
            "expiry":    expiry,
            "dte":       _dte(expiry),
        })
    # Prefer near ATM, high volume
    return sorted(results, key=lambda r: (abs(r["strike"] - price), -r["volume"]))[:4]


def find_long_puts(price: float, chain: list[dict],
                   budget: float, expiry: str) -> list[dict]:
    """Single-leg long puts within budget, ATM to 10% OTM."""
    results = []
    for leg in chain:
        k = leg["strike"]
        if k > price * 1.02 or k < price * 0.88:
            continue
        cost = round(leg["ask"] * 100, 2)
        if cost > budget:
            continue
        be     = round(k - leg["ask"], 2)
        be_pct = round((be - price) / price * 100, 1)
        results.append({
            "type":      "long_put",
            "label":     f"${k:.0f}p",
            "strike":    k,
            "ask":       leg["ask"],
            "mid":       leg["mid"],
            "cost":      cost,
            "breakeven": be,
            "be_move_pct": be_pct,
            "iv":        leg["iv"],
            "volume":    leg["volume"],
            "oi":        leg["oi"],
            "expiry":    expiry,
            "dte":       _dte(expiry),
        })
    return sorted(results, key=lambda r: (abs(r["strike"] - price), -r["volume"]))[:4]


# ── Main entry point ──────────────────────────────────────────────────────────

def find_options(ticker: str, budget: float = 1000,
                 mode: str = "all") -> dict:
    """
    Find the best options plays for a given ticker and budget.

    mode: "all" | "calls" | "puts" | "spreads" | "long"

    Returns dict:
        {
          "ticker": str,
          "price": float,
          "expiry": str,
          "dte": int,
          "call_spreads": [...],
          "put_spreads": [...],
          "long_calls": [...],
          "long_puts": [...],
          "error": str | None,
        }
    """
    import yfinance as yf

    result = {
        "ticker": ticker.upper(),
        "price":  0.0,
        "expiry": "",
        "dte":    0,
        "call_spreads": [],
        "put_spreads":  [],
        "long_calls":   [],
        "long_puts":    [],
        "error": None,
    }

    try:
        tk    = yf.Ticker(ticker)
        price = float(tk.fast_info.get("lastPrice") or tk.fast_info.get("regularMarketPrice") or 0)
        if not price:
            result["error"] = f"Could not get price for {ticker}"
            return result
        result["price"] = round(price, 2)

        exps = tk.options
        if not exps:
            result["error"] = f"No options data available for {ticker}"
            return result

        expiry = _best_expiry(exps, min_dte=21, target_dte=40)
        if not expiry:
            result["error"] = f"No valid expiry found for {ticker} (need ≥21 DTE)"
            return result

        result["expiry"] = expiry
        result["dte"]    = _dte(expiry)

        want_calls   = mode in ("all", "calls", "spreads", "long")
        want_puts    = mode in ("all", "puts", "spreads", "long")
        want_spreads = mode in ("all", "calls", "puts", "spreads")
        want_long    = mode in ("all", "calls", "puts", "long")

        if want_calls or want_spreads:
            calls = get_chain(ticker, expiry, "call")
            if want_spreads:
                result["call_spreads"] = find_call_spreads(ticker, price, calls, budget, expiry)
            if want_long and mode in ("all", "calls", "long"):
                result["long_calls"] = find_long_calls(price, calls, budget, expiry)

        if want_puts or want_spreads:
            puts = get_chain(ticker, expiry, "put")
            if want_spreads:
                result["put_spreads"] = find_put_spreads(ticker, price, puts, budget, expiry)
            if want_long and mode in ("all", "puts", "long"):
                result["long_puts"] = find_long_puts(price, puts, budget, expiry)

    except Exception as e:
        result["error"] = str(e)

    return result


# ── Discord embed builder ─────────────────────────────────────────────────────

def build_discord_embeds(data: dict, budget: float) -> list:
    """Convert find_options() result into a list of Discord embed dicts."""
    ticker  = data["ticker"]
    price   = data["price"]
    expiry  = data["expiry"]
    dte     = data["dte"]

    if data.get("error"):
        return [{"title": f"Options Finder — {ticker}",
                 "description": f"Error: {data['error']}",
                 "color": 0xf87171}]

    embeds = []

    # ── Header embed ──────────────────────────────────────────────────────────
    header = {
        "title": f"Options Finder — {ticker}",
        "description": (
            f"**Price:** ${price:,.2f}  |  "
            f"**Budget:** ${budget:,.0f}  |  "
            f"**Expiry:** {expiry} ({dte} DTE)"
        ),
        "color": 0x7c6af7,
        "fields": [],
    }

    # ── Bull call spreads ──────────────────────────────────────────────────────
    if data["call_spreads"]:
        lines = []
        for r in data["call_spreads"][:3]:
            lines.append(
                f"**{r['label']}  exp {r['expiry']} ({r['dte']}d)**  "
                f"cost=${r['cost']:.0f}  max=${r['max_profit']:.0f}  "
                f"RR {r['rr']:.1f}x  BE ${r['breakeven']:.2f} ({r['be_move_pct']:+.1f}%)  "
                f"OI={r['min_oi']}  vol={r['min_vol']}"
            )
        header["fields"].append({
            "name": "📈 Bull Call Spreads",
            "value": "\n".join(lines),
            "inline": False,
        })

    # ── Bear put spreads ──────────────────────────────────────────────────────
    if data["put_spreads"]:
        lines = []
        for r in data["put_spreads"][:3]:
            lines.append(
                f"**{r['label']}  exp {r['expiry']} ({r['dte']}d)**  "
                f"cost=${r['cost']:.0f}  max=${r['max_profit']:.0f}  "
                f"RR {r['rr']:.1f}x  BE ${r['breakeven']:.2f} ({r['be_move_pct']:+.1f}%)  "
                f"OI={r['min_oi']}  vol={r['min_vol']}"
            )
        header["fields"].append({
            "name": "📉 Bear Put Spreads",
            "value": "\n".join(lines),
            "inline": False,
        })

    # ── Long calls ─────────────────────────────────────────────────────────────
    if data["long_calls"]:
        lines = []
        for r in data["long_calls"][:3]:
            lines.append(
                f"**{r['label']}  exp {r['expiry']} ({r['dte']}d)**  "
                f"ask=${r['ask']:.2f} (${r['cost']:.0f})  "
                f"IV={r['iv']:.0%}  BE ${r['breakeven']:.2f} ({r['be_move_pct']:+.1f}%)  "
                f"vol={r['volume']:,}"
            )
        header["fields"].append({
            "name": "☎️ Long Calls",
            "value": "\n".join(lines),
            "inline": False,
        })

    # ── Long puts ─────────────────────────────────────────────────────────────
    if data["long_puts"]:
        lines = []
        for r in data["long_puts"][:3]:
            lines.append(
                f"**{r['label']}  exp {r['expiry']} ({r['dte']}d)**  "
                f"ask=${r['ask']:.2f} (${r['cost']:.0f})  "
                f"IV={r['iv']:.0%}  BE ${r['breakeven']:.2f} ({r['be_move_pct']:+.1f}%)  "
                f"vol={r['volume']:,}"
            )
        header["fields"].append({
            "name": "☎️ Long Puts",
            "value": "\n".join(lines),
            "inline": False,
        })

    if not header["fields"]:
        header["description"] += f"\n\nNo plays found within ${budget:,.0f} budget."

    header["footer"] = {"text": "Not financial advice  •  Always DYOR  •  Conquest Trading"}
    embeds.append(header)
    return embeds


# ── CLI usage ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else "AAPL"
    budget = float(sys.argv[2]) if len(sys.argv) > 2 else 1000.0
    mode   = sys.argv[3].lower() if len(sys.argv) > 3 else "all"

    print(f"\nSearching {ticker} options (budget ${budget:.0f}, mode={mode})...\n")
    data = find_options(ticker, budget, mode)

    if data.get("error"):
        print(f"Error: {data['error']}")
        sys.exit(1)

    print(f"{ticker} @ ${data['price']:.2f}  |  Expiry: {data['expiry']} ({data['dte']} DTE)\n")

    if data["call_spreads"]:
        print("-- Bull Call Spreads -------------------------------------------")
        for r in data["call_spreads"]:
            print(f"  {r['label']:20s}  exp {r['expiry']} ({r['dte']}d)  "
                  f"cost=${r['cost']:>6.0f}  max=${r['max_profit']:>6.0f}  "
                  f"RR={r['rr']:.1f}x  BE=${r['breakeven']:.2f} ({r['be_move_pct']:+.1f}%)  "
                  f"OI={r['min_oi']}  vol={r['min_vol']}")

    if data["put_spreads"]:
        print("\n-- Bear Put Spreads --------------------------------------------")
        for r in data["put_spreads"]:
            print(f"  {r['label']:20s}  exp {r['expiry']} ({r['dte']}d)  "
                  f"cost=${r['cost']:>6.0f}  max=${r['max_profit']:>6.0f}  "
                  f"RR={r['rr']:.1f}x  BE=${r['breakeven']:.2f} ({r['be_move_pct']:+.1f}%)  "
                  f"OI={r['min_oi']}  vol={r['min_vol']}")

    if data["long_calls"]:
        print("\n-- Long Calls --------------------------------------------------")
        for r in data["long_calls"]:
            print(f"  {r['label']:12s}  exp {r['expiry']} ({r['dte']}d)  "
                  f"ask=${r['ask']:.2f} (${r['cost']:.0f})  "
                  f"IV={r['iv']:.0%}  BE=${r['breakeven']:.2f} ({r['be_move_pct']:+.1f}%)  "
                  f"vol={r['volume']:,}")

    if data["long_puts"]:
        print("\n-- Long Puts ---------------------------------------------------")
        for r in data["long_puts"]:
            print(f"  {r['label']:12s}  exp {r['expiry']} ({r['dte']}d)  "
                  f"ask=${r['ask']:.2f} (${r['cost']:.0f})  "
                  f"IV={r['iv']:.0%}  BE=${r['breakeven']:.2f} ({r['be_move_pct']:+.1f}%)  "
                  f"vol={r['volume']:,}")
