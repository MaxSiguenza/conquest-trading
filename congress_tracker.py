# -*- coding: utf-8 -*-
"""
congress_tracker.py — Conquest Trading
========================================
Congressional stock trade tracker using public STOCK Act disclosure data.

Data sources (free, no API key required):
  - House: https://housestockwatcher.com/api
  - Senate: https://senatestockwatcher.com/api

Under the STOCK Act (2012), members of Congress must disclose stock trades
within 30-45 days of execution. This data is public record.

Usage:
    from congress_tracker import recent_trades, trades_for_ticker, watchlist_trades
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Optional

import requests

# ── API endpoints ──────────────────────────────────────────────────────────────
_HOUSE_API  = "https://housestockwatcher.com/api"
_SENATE_API = "https://senatestockwatcher.com/api"
_TIMEOUT    = 15


# ── Amount range mid-points (for sorting/display) ─────────────────────────────
_AMOUNT_ORDER = {
    "$1,001 - $15,000":         1,
    "$15,001 - $50,000":        2,
    "$50,001 - $100,000":       3,
    "$100,001 - $250,000":      4,
    "$250,001 - $500,000":      5,
    "$500,001 - $1,000,000":    6,
    "$1,000,001 - $5,000,000":  7,
    "$5,000,001 - $25,000,000": 8,
    "Over $25,000,000":         9,
}

_AMOUNT_LABEL = {
    "$1,001 - $15,000":         "<$15k",
    "$15,001 - $50,000":        "$15k–50k",
    "$50,001 - $100,000":       "$50k–100k",
    "$100,001 - $250,000":      "$100k–250k",
    "$250,001 - $500,000":      "$250k–500k",
    "$500,001 - $1,000,000":    "$500k–1M",
    "$1,000,001 - $5,000,000":  "$1M–5M",
    "$5,000,001 - $25,000,000": "$5M–25M",
    "Over $25,000,000":         ">$25M",
}


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            pass
    return None


def _clean_ticker(raw: str) -> str:
    """Normalise ticker — strip whitespace, $, and trailing garbage."""
    if not raw:
        return ""
    t = raw.strip().upper().replace("$", "").split()[0]
    # Keep only valid ticker characters
    t = re.sub(r"[^A-Z0-9\.]", "", t)
    return t


def _normalize_house(raw: dict) -> dict:
    """Map a House API row to the standard trade dict."""
    ticker = _clean_ticker(raw.get("ticker") or "")
    tx_type = (raw.get("type") or "").lower()
    if "purchase" in tx_type:
        action = "buy"
    elif "sale" in tx_type or "sold" in tx_type:
        action = "sell"
    elif "exchange" in tx_type:
        action = "exchange"
    else:
        action = tx_type or "unknown"

    amount_raw = raw.get("amount") or ""
    return {
        "chamber":          "House",
        "member":           raw.get("representative") or "",
        "state":            raw.get("state") or "",
        "party":            (raw.get("party") or "")[:1].upper(),   # R / D / I
        "ticker":           ticker,
        "asset":            raw.get("asset_description") or "",
        "asset_type":       raw.get("asset_type") or "",
        "action":           action,
        "amount_raw":       amount_raw,
        "amount_label":     _AMOUNT_LABEL.get(amount_raw, amount_raw),
        "amount_order":     _AMOUNT_ORDER.get(amount_raw, 0),
        "transaction_date": _parse_date(raw.get("transaction_date") or ""),
        "disclosure_date":  _parse_date(raw.get("disclosure_date") or ""),
        "comment":          raw.get("comment") or "",
    }


def _normalize_senate(raw: dict) -> dict:
    """Map a Senate API row to the standard trade dict."""
    ticker = _clean_ticker(raw.get("ticker") or "")
    tx_type = (raw.get("type") or "").lower()
    if "purchase" in tx_type:
        action = "buy"
    elif "sale" in tx_type or "sold" in tx_type:
        action = "sell"
    elif "exchange" in tx_type:
        action = "exchange"
    else:
        action = tx_type or "unknown"

    amount_raw = raw.get("amount") or ""
    return {
        "chamber":          "Senate",
        "member":           raw.get("senator") or "",
        "state":            raw.get("state") or "",
        "party":            (raw.get("party") or "")[:1].upper(),
        "ticker":           ticker,
        "asset":            raw.get("asset_description") or "",
        "asset_type":       raw.get("asset_type") or "",
        "action":           action,
        "amount_raw":       amount_raw,
        "amount_label":     _AMOUNT_LABEL.get(amount_raw, amount_raw),
        "amount_order":     _AMOUNT_ORDER.get(amount_raw, 0),
        "transaction_date": _parse_date(raw.get("transaction_date") or ""),
        "disclosure_date":  _parse_date(raw.get("disclosure_date") or ""),
        "comment":          raw.get("comment") or "",
    }


# ── Raw fetchers ───────────────────────────────────────────────────────────────

def _fetch_house(limit: int = 500) -> list[dict]:
    """Fetch latest House disclosures. Returns normalized trade dicts."""
    try:
        resp = requests.get(_HOUSE_API, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        # API may return {"data": [...]} or just [...]
        rows = data.get("data", data) if isinstance(data, dict) else data
        normalized = [_normalize_house(r) for r in rows[:limit] if isinstance(r, dict)]
        return [t for t in normalized if t["ticker"]]
    except Exception as e:
        print(f"[Congress] House API error: {e}")
        return []


def _fetch_senate(limit: int = 500) -> list[dict]:
    """Fetch latest Senate disclosures. Returns normalized trade dicts."""
    try:
        resp = requests.get(_SENATE_API, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("data", data) if isinstance(data, dict) else data
        normalized = [_normalize_senate(r) for r in rows[:limit] if isinstance(r, dict)]
        return [t for t in normalized if t["ticker"]]
    except Exception as e:
        print(f"[Congress] Senate API error: {e}")
        return []


# ── Public API ─────────────────────────────────────────────────────────────────

def recent_trades(days: int = 30, actions: Optional[list[str]] = None) -> list[dict]:
    """
    Return all congressional trades from the last `days` calendar days,
    combined from House + Senate, sorted newest first.

    actions: filter to ["buy"], ["sell"], or None for all.
    """
    cutoff = date.today() - timedelta(days=days)
    trades = _fetch_house() + _fetch_senate()

    result = []
    for t in trades:
        tx_date = t["transaction_date"]
        if tx_date and tx_date >= cutoff:
            if actions is None or t["action"] in actions:
                result.append(t)

    result.sort(key=lambda x: x["transaction_date"] or date.min, reverse=True)
    return result


def trades_for_ticker(ticker: str, days: int = 90) -> list[dict]:
    """All congressional trades for a specific ticker in the last `days` days."""
    ticker = ticker.upper().strip()
    cutoff = date.today() - timedelta(days=days)
    trades = _fetch_house() + _fetch_senate()
    result = [
        t for t in trades
        if t["ticker"] == ticker
        and t["transaction_date"]
        and t["transaction_date"] >= cutoff
    ]
    result.sort(key=lambda x: x["transaction_date"] or date.min, reverse=True)
    return result


def watchlist_trades(tickers: list[str], days: int = 14) -> list[dict]:
    """
    All congressional trades for tickers in `tickers` list, last `days` days.
    Returns sorted by transaction_date desc, then by amount_order desc.
    """
    universe = {t.upper() for t in tickers}
    cutoff   = date.today() - timedelta(days=days)
    trades   = _fetch_house() + _fetch_senate()
    result   = [
        t for t in trades
        if t["ticker"] in universe
        and t["transaction_date"]
        and t["transaction_date"] >= cutoff
    ]
    result.sort(key=lambda x: (
        x["transaction_date"] or date.min,
        x["amount_order"]
    ), reverse=True)
    return result


def top_purchased_tickers(days: int = 14, n: int = 10) -> list[dict]:
    """
    Return the top `n` most-purchased tickers by Congress in the last `days` days.
    Each entry: {ticker, buy_count, sell_count, net_buys, total_trades, members}
    """
    trades = recent_trades(days=days)
    agg: dict[str, dict] = {}
    for t in trades:
        tk = t["ticker"]
        if tk not in agg:
            agg[tk] = {"ticker": tk, "buy_count": 0, "sell_count": 0, "members": set()}
        if t["action"] == "buy":
            agg[tk]["buy_count"] += 1
        elif t["action"] == "sell":
            agg[tk]["sell_count"] += 1
        if t["member"]:
            agg[tk]["members"].add(t["member"])

    result = []
    for tk, v in agg.items():
        result.append({
            "ticker":       tk,
            "buy_count":    v["buy_count"],
            "sell_count":   v["sell_count"],
            "net_buys":     v["buy_count"] - v["sell_count"],
            "total_trades": v["buy_count"] + v["sell_count"],
            "member_count": len(v["members"]),
            "members":      sorted(v["members"]),
        })

    # Sort by net buys desc, then total trades desc
    result.sort(key=lambda x: (x["net_buys"], x["total_trades"]), reverse=True)
    return result[:n]


def summary_stats(days: int = 14) -> dict:
    """High-level summary: total trades, top buyers, most active members."""
    trades  = recent_trades(days=days)
    buys    = [t for t in trades if t["action"] == "buy"]
    sells   = [t for t in trades if t["action"] == "sell"]

    member_counts: dict[str, int] = {}
    for t in trades:
        m = t["member"]
        if m:
            member_counts[m] = member_counts.get(m, 0) + 1

    most_active = sorted(member_counts.items(), key=lambda x: -x[1])[:5]

    return {
        "days":          days,
        "total_trades":  len(trades),
        "buy_count":     len(buys),
        "sell_count":    len(sells),
        "house_trades":  sum(1 for t in trades if t["chamber"] == "House"),
        "senate_trades": sum(1 for t in trades if t["chamber"] == "Senate"),
        "most_active":   [{"member": m, "trades": c} for m, c in most_active],
    }
