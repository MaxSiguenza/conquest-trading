# -*- coding: utf-8 -*-
"""
Compare current backtest option-pricing assumptions against live option chains.

This does not turn the historical backtest into a true historical options-chain
backtest. It audits whether today's Black-Scholes/HV model is in the same
ballpark as today's live chain for the structures the backtest simulates.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from statistics import mean, median

import yfinance as yf

from backtest import DTE_TARGET, IV_PREMIUM_FACTOR, RISK_FREE, _bs_price, _hist_vol
from universe_screener import SP500_UNIVERSE


def _mid(row) -> float | None:
    bid = float(row.get("bid") or 0)
    ask = float(row.get("ask") or 0)
    last = float(row.get("lastPrice") or 0)
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 4)
    if last > 0:
        return round(last, 4)
    if ask > 0:
        return round(ask, 4)
    return None


def _expiry(tk, target_dte: int = DTE_TARGET) -> str | None:
    today = date.today()
    exps = list(tk.options or [])
    valid = [e for e in exps if (date.fromisoformat(e) - today).days >= 14]
    if not valid:
        return None
    return min(valid, key=lambda e: abs((date.fromisoformat(e) - today).days - target_dte))


def _quote(df, target: float, *, min_strike=None, max_strike=None) -> dict | None:
    if df is None or df.empty:
        return None
    pool = df.copy()
    pool["strike"] = pool["strike"].astype(float)
    if min_strike is not None:
        pool = pool[pool["strike"] >= float(min_strike)]
    if max_strike is not None:
        pool = pool[pool["strike"] <= float(max_strike)]
    if pool.empty:
        return None
    liquid = pool[pool["ask"].fillna(0) > 0]
    pool = liquid if not liquid.empty else pool
    idx = (pool["strike"] - float(target)).abs().idxmin()
    row = pool.loc[idx]
    mid = _mid(row)
    if mid is None or mid <= 0:
        return None
    bid = float(row.get("bid") or 0)
    ask = float(row.get("ask") or 0)
    return {
        "strike": float(row["strike"]),
        "bid": round(bid, 4),
        "ask": round(ask, 4),
        "mid": mid,
        "iv": round(float(row.get("impliedVolatility") or 0), 4),
        "contract_symbol": str(row.get("contractSymbol") or ""),
    }


def _pct_diff(model: float, live: float) -> float | None:
    if live <= 0:
        return None
    return round((model - live) / live * 100, 2)


def audit_ticker(ticker: str) -> list[dict]:
    tk = yf.Ticker(ticker)
    hist = tk.history(period="90d", auto_adjust=False)
    if hist is None or hist.empty or len(hist) < 35:
        return [{"ticker": ticker, "error": "insufficient_history"}]

    spot = float(hist["Close"].dropna().iloc[-1])
    closes = hist["Close"].dropna().values
    hv = _hist_vol(closes)
    model_iv = hv * IV_PREMIUM_FACTOR
    exp = _expiry(tk)
    if not exp:
        return [{"ticker": ticker, "error": "no_valid_expiry", "spot": round(spot, 2)}]

    dte = (date.fromisoformat(exp) - date.today()).days
    T = max(dte / 365, 0.001)
    chain = tk.option_chain(exp)
    calls, puts = chain.calls, chain.puts

    rows = []

    for name, opt_type, target, df in (
        ("long_call", "call", spot * 1.02, calls),
        ("long_put", "put", spot * 0.98, puts),
    ):
        q = _quote(df, target)
        if not q:
            rows.append({"ticker": ticker, "strategy": name, "error": "no_live_quote"})
            continue
        model = round(_bs_price(spot, q["strike"], T, RISK_FREE, model_iv, opt_type), 4)
        rows.append({
            "ticker": ticker,
            "strategy": name,
            "spot": round(spot, 2),
            "expiry": exp,
            "dte": dte,
            "strike": q["strike"],
            "contract": q["contract_symbol"],
            "live_mid": q["mid"],
            "model_mid": model,
            "model_vs_live_pct": _pct_diff(model, q["mid"]),
            "live_iv": q["iv"],
            "model_iv": round(model_iv, 4),
            "bid": q["bid"],
            "ask": q["ask"],
        })

    short_put = _quote(puts, spot * 0.97)
    if short_put:
        long_put = _quote(puts, short_put["strike"] - abs(spot * 0.03), max_strike=short_put["strike"] - 0.01)
        if long_put:
            live_credit = round(short_put["mid"] - long_put["mid"], 4)
            model_short = _bs_price(spot, short_put["strike"], T, RISK_FREE, model_iv, "put")
            model_long = _bs_price(spot, long_put["strike"], T, RISK_FREE, model_iv, "put")
            model_credit = round(model_short - model_long, 4)
            width = abs(short_put["strike"] - long_put["strike"])
            rows.append({
                "ticker": ticker,
                "strategy": "bull_put_spread",
                "spot": round(spot, 2),
                "expiry": exp,
                "dte": dte,
                "short_strike": short_put["strike"],
                "long_strike": long_put["strike"],
                "width": width,
                "live_credit": live_credit,
                "model_credit": model_credit,
                "model_vs_live_pct": _pct_diff(model_credit, live_credit),
                "short_contract": short_put["contract_symbol"],
                "long_contract": long_put["contract_symbol"],
                "model_iv": round(model_iv, 4),
                "avg_live_iv": round(mean([short_put["iv"], long_put["iv"]]), 4),
            })
        else:
            rows.append({"ticker": ticker, "strategy": "bull_put_spread", "error": "missing_long_leg"})
    else:
        rows.append({"ticker": ticker, "strategy": "bull_put_spread", "error": "missing_short_leg"})

    cov_call = _quote(calls, spot * 1.03)
    if cov_call:
        model = round(_bs_price(spot, cov_call["strike"], T, RISK_FREE, model_iv, "call"), 4)
        rows.append({
            "ticker": ticker,
            "strategy": "covered_call_short_call",
            "spot": round(spot, 2),
            "expiry": exp,
            "dte": dte,
            "strike": cov_call["strike"],
            "contract": cov_call["contract_symbol"],
            "live_mid": cov_call["mid"],
            "model_mid": model,
            "model_vs_live_pct": _pct_diff(model, cov_call["mid"]),
            "live_iv": cov_call["iv"],
            "model_iv": round(model_iv, 4),
            "bid": cov_call["bid"],
            "ask": cov_call["ask"],
        })
    else:
        rows.append({"ticker": ticker, "strategy": "covered_call_short_call", "error": "no_live_quote"})

    return rows


def summarize(rows: list[dict]) -> dict:
    usable = [r for r in rows if r.get("model_vs_live_pct") is not None]
    diffs = [r["model_vs_live_pct"] for r in usable]
    by_strategy = {}
    for r in usable:
        by_strategy.setdefault(r["strategy"], []).append(r["model_vs_live_pct"])
    no_tiny_spread_credit = [
        r for r in usable
        if not (r.get("live_credit") is not None and abs(float(r.get("live_credit") or 0)) < 0.25)
    ]
    no_tiny_diffs = [r["model_vs_live_pct"] for r in no_tiny_spread_credit]
    return {
        "audit_time": datetime.now().isoformat(timespec="seconds"),
        "note": (
            "Compares the current Black-Scholes/HV backtest model with current live option-chain mids. "
            "This is not a historical options-chain backtest."
        ),
        "quotes_compared": len(usable),
        "errors": len([r for r in rows if r.get("error")]),
        "avg_model_vs_live_pct": round(mean(diffs), 2) if diffs else None,
        "median_model_vs_live_pct": round(median(diffs), 2) if diffs else None,
        "avg_without_tiny_spread_credits_pct": round(mean(no_tiny_diffs), 2) if no_tiny_diffs else None,
        "tiny_spread_credit_rows": len(usable) - len(no_tiny_spread_credit),
        "by_strategy": {
            k: {
                "count": len(v),
                "avg_model_vs_live_pct": round(mean(v), 2),
                "median_model_vs_live_pct": round(median(v), 2),
            }
            for k, v in sorted(by_strategy.items())
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+")
    parser.add_argument(
        "--default-universe",
        action="store_true",
        help="Audit the same default universe used by backtest.py.",
    )
    parser.add_argument("--output")
    args = parser.parse_args()

    tickers = SP500_UNIVERSE if args.default_universe else args.tickers
    if not tickers:
        parser.error("provide --tickers or --default-universe")

    rows = []
    for ticker in tickers:
        print(f"Auditing {ticker}...")
        rows.extend(audit_ticker(ticker))

    summary = summarize(rows)
    result = {"summary": summary, "rows": rows}
    print(json.dumps(summary, indent=2))
    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
