# -*- coding: utf-8 -*-
"""
congress_tracker.py — Conquest Trading
========================================
Congressional stock trade tracker using public STOCK Act disclosure data.

Data sources tried in order (all free, no API key):
  Primary:  housestockwatcher.com/api  +  senatestockwatcher.com/api
  Fallback: GitHub-mirrored CSV snapshots (House Clerk / Senate)

Under the STOCK Act (2012), members of Congress must disclose stock trades
within 30-45 days of execution. This is fully public record.

Usage:
    from congress_tracker import recent_trades, trades_for_ticker, watchlist_trades, debug_raw
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Optional

import requests

# ── API endpoints (tried in order with fallbacks) ─────────────────────────────
_HOUSE_ENDPOINTS = [
    "https://housestockwatcher.com/api",
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
]
_SENATE_ENDPOINTS = [
    "https://senatestockwatcher.com/api",
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json",
]
_TIMEOUT = 15
_HEADERS = {"User-Agent": "ConquestTrading/1.0 (contact: github.com/conquest-trading)"}


# ── Amount display ─────────────────────────────────────────────────────────────
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


# ── Date parsing ───────────────────────────────────────────────────────────────
def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(s)[:10], fmt).date()
        except ValueError:
            pass
    return None


# ── Ticker extraction ──────────────────────────────────────────────────────────
def _extract_ticker(ticker_raw: str, description: str = "") -> str:
    """
    Extract a clean ticker from the raw ticker field or asset description.
    Handles: 'NVDA', 'nvda', 'NVDA ', '--', 'N/A', '$NVDA',
             'NVIDIA Corporation (NVDA)', 'Call Option NVDA', etc.
    """
    # Try the direct ticker field first
    if ticker_raw and ticker_raw.strip() not in ("--", "N/A", "n/a", "", "nan"):
        t = ticker_raw.strip().upper().replace("$", "")
        # Strip option suffix like "NVDA230120C..." — keep root symbol
        t = re.split(r'\d{6}[CP]', t)[0]
        t = re.sub(r"[^A-Z0-9\.\-]", "", t).strip("-.")
        if 1 <= len(t) <= 6:
            return t

    # Try to extract from asset description e.g. "NVIDIA Corp (NVDA)"
    if description:
        m = re.search(r'\(([A-Z]{1,6})\)', description.upper())
        if m:
            return m.group(1)
        # "Stock: NVDA" or "Call Option NVDA"
        m = re.search(r'\b([A-Z]{2,6})\b', description.upper())
        if m and m.group(1) not in ("CALL", "PUT", "CORP", "INC", "ETF",
                                     "LLC", "THE", "AND", "FOR", "NEW"):
            return m.group(1)
    return ""


# ── Action normalisation ───────────────────────────────────────────────────────
def _parse_action(raw: str) -> str:
    r = (raw or "").lower()
    if "purchase" in r or "buy" in r:
        return "buy"
    if "sale_full" in r or "sale_partial" in r or "sale" in r or "sell" in r or "sold" in r:
        return "sell"
    if "exchange" in r:
        return "exchange"
    return raw.strip().lower() or "unknown"


# ── Row normalizers ────────────────────────────────────────────────────────────
def _normalize_house(raw: dict) -> dict:
    description = raw.get("asset_description") or ""
    ticker      = _extract_ticker(raw.get("ticker") or "", description)
    amount_raw  = raw.get("amount") or ""
    return {
        "chamber":          "House",
        "member":           (raw.get("representative") or raw.get("name") or "").strip(),
        "state":            raw.get("state") or "",
        "party":            (raw.get("party") or "")[:1].upper(),
        "ticker":           ticker,
        "asset":            description,
        "asset_type":       raw.get("asset_type") or "",
        "action":           _parse_action(raw.get("type") or raw.get("transaction_type") or ""),
        "amount_raw":       amount_raw,
        "amount_label":     _AMOUNT_LABEL.get(amount_raw, amount_raw or "?"),
        "amount_order":     _AMOUNT_ORDER.get(amount_raw, 0),
        "transaction_date": _parse_date(raw.get("transaction_date") or raw.get("date") or ""),
        "disclosure_date":  _parse_date(raw.get("disclosure_date") or raw.get("filed_date") or ""),
        "comment":          raw.get("comment") or "",
    }


def _normalize_senate(raw: dict) -> dict:
    description = raw.get("asset_description") or ""
    ticker      = _extract_ticker(
        raw.get("ticker") or raw.get("asset_ticker") or "",
        description,
    )
    amount_raw  = raw.get("amount") or ""
    return {
        "chamber":          "Senate",
        "member":           (raw.get("senator") or raw.get("first_name", "") + " " + raw.get("last_name", "") or "").strip(),
        "state":            raw.get("state") or "",
        "party":            (raw.get("party") or "")[:1].upper(),
        "ticker":           ticker,
        "asset":            description,
        "asset_type":       raw.get("asset_type") or "",
        "action":           _parse_action(raw.get("type") or raw.get("transaction_type") or ""),
        "amount_raw":       amount_raw,
        "amount_label":     _AMOUNT_LABEL.get(amount_raw, amount_raw or "?"),
        "amount_order":     _AMOUNT_ORDER.get(amount_raw, 0),
        "transaction_date": _parse_date(raw.get("transaction_date") or raw.get("date") or ""),
        "disclosure_date":  _parse_date(raw.get("disclosure_date") or raw.get("filed_date") or ""),
        "comment":          raw.get("comment") or "",
    }


# ── Raw fetchers ───────────────────────────────────────────────────────────────
def _fetch_from_endpoints(endpoints: list[str], normalizer, limit: int = 2000) -> tuple[list[dict], str]:
    """
    Try each endpoint in order. Returns (normalized_trades, source_url_used).
    """
    last_err = ""
    for url in endpoints:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            # Handle both {"data": [...]} and direct [...]
            if isinstance(data, dict):
                rows = data.get("data") or data.get("transactions") or data.get("results") or []
            elif isinstance(data, list):
                rows = data
            else:
                rows = []

            normalized = []
            for r in rows[:limit]:
                if not isinstance(r, dict):
                    continue
                try:
                    n = normalizer(r)
                    if n["ticker"]:          # skip rows with no identifiable ticker
                        normalized.append(n)
                except Exception:
                    pass

            print(f"[Congress] {url} → {len(normalized)} trades with tickers (from {len(rows)} raw rows)")
            return normalized, url

        except Exception as e:
            last_err = str(e)
            print(f"[Congress] {url} failed: {e}")
            continue

    print(f"[Congress] All endpoints failed. Last error: {last_err}")
    return [], ""


def _fetch_house(limit: int = 2000) -> list[dict]:
    trades, _ = _fetch_from_endpoints(_HOUSE_ENDPOINTS, _normalize_house, limit)
    return trades


def _fetch_senate(limit: int = 2000) -> list[dict]:
    trades, _ = _fetch_from_endpoints(_SENATE_ENDPOINTS, _normalize_senate, limit)
    return trades


# ── Debug helper ───────────────────────────────────────────────────────────────
def debug_raw(n: int = 5) -> dict:
    """
    Return raw API samples for debugging.
    Shows first N raw records from House and Senate before any normalisation.
    Call from !congressdebug command.
    """
    result = {"house": {}, "senate": {}}

    for url in _HOUSE_ENDPOINTS:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("data", data) if isinstance(data, dict) else data
            result["house"] = {
                "url":         url,
                "total_rows":  len(rows),
                "sample_keys": list(rows[0].keys()) if rows else [],
                "samples":     rows[:n],
                "sample_tickers": list({str(r.get("ticker","")).strip() for r in rows[:100]}),
            }
            break
        except Exception as e:
            result["house"][url] = str(e)

    for url in _SENATE_ENDPOINTS:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("data", data) if isinstance(data, dict) else data
            result["senate"] = {
                "url":         url,
                "total_rows":  len(rows),
                "sample_keys": list(rows[0].keys()) if rows else [],
                "samples":     rows[:n],
                "sample_tickers": list({str(r.get("ticker","")).strip() for r in rows[:100]}),
            }
            break
        except Exception as e:
            result["senate"][url] = str(e)

    return result


# ── Public API ─────────────────────────────────────────────────────────────────
def recent_trades(days: int = 30, actions: Optional[list[str]] = None) -> list[dict]:
    """All trades from the last `days` days, House + Senate, newest first."""
    cutoff = date.today() - timedelta(days=days)
    trades = _fetch_house() + _fetch_senate()
    result = [
        t for t in trades
        if t["transaction_date"] and t["transaction_date"] >= cutoff
        and (actions is None or t["action"] in actions)
    ]
    result.sort(key=lambda x: x["transaction_date"] or date.min, reverse=True)
    return result


def trades_for_ticker(ticker: str, days: int = 365) -> list[dict]:
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
    """Congressional trades for tickers in the given list, last `days` days."""
    universe = {t.upper() for t in tickers}
    cutoff   = date.today() - timedelta(days=days)
    trades   = _fetch_house() + _fetch_senate()
    result   = [
        t for t in trades
        if t["ticker"] in universe
        and t["transaction_date"]
        and t["transaction_date"] >= cutoff
    ]
    result.sort(
        key=lambda x: (x["transaction_date"] or date.min, x["amount_order"]),
        reverse=True,
    )
    return result


def top_purchased_tickers(days: int = 14, n: int = 10) -> list[dict]:
    """Top `n` most net-purchased tickers by Congress in the last `days` days."""
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

    result = [{
        "ticker":       tk,
        "buy_count":    v["buy_count"],
        "sell_count":   v["sell_count"],
        "net_buys":     v["buy_count"] - v["sell_count"],
        "total_trades": v["buy_count"] + v["sell_count"],
        "member_count": len(v["members"]),
        "members":      sorted(v["members"]),
    } for tk, v in agg.items()]

    result.sort(key=lambda x: (x["net_buys"], x["total_trades"]), reverse=True)
    return result[:n]


def summary_stats(days: int = 14) -> dict:
    """High-level stats for the last `days` days."""
    trades = recent_trades(days=days)
    buys   = [t for t in trades if t["action"] == "buy"]
    sells  = [t for t in trades if t["action"] == "sell"]
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
