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
import os
import re
from datetime import date, datetime, timedelta
from typing import Optional

import requests

# ── API endpoints (tried in order with fallbacks) ─────────────────────────────
_HOUSE_ENDPOINTS = [
    "https://housestockwatcher.com/api",
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
    "https://raw.githubusercontent.com/timothycarambat/house-stock-watcher-data/master/data/all_transactions.json",
]
_SENATE_ENDPOINTS = [
    "https://senatestockwatcher.com/api",
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json",
    "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json",
]
_QUIVER_ENDPOINTS = [
    "https://api.quiverquant.com/beta/bulk/congress/trading",
    "https://api.quiverquant.com/beta/bulk/congress/trades",
    "https://api.quiverquant.com/beta/live/congresstrading",
    "https://api.quiverquant.com/beta/bulk/congresstrading",
]
_TIMEOUT = 15
_HEADERS = {"User-Agent": "ConquestTrading/1.0 (contact: github.com/conquest-trading)"}


def _env(name: str) -> str:
    val = os.getenv(name, "")
    if val:
        return val
    try:
        from dotenv import dotenv_values
        return dotenv_values(os.path.join(os.path.dirname(__file__), ".env")).get(name, "") or ""
    except Exception:
        return ""


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
    text = str(s).strip()
    candidates = [text, text[:10]]
    for value in candidates:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(value, fmt).date()
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


def _normalize_quiver(raw: dict) -> dict:
    ticker = _extract_ticker(
        raw.get("Ticker") or raw.get("ticker") or raw.get("Symbol") or raw.get("symbol") or "",
        raw.get("Asset") or raw.get("asset") or raw.get("AssetDescription") or raw.get("asset_description") or "",
    )
    member = (
        raw.get("Representative") or raw.get("representative") or
        raw.get("Politician") or raw.get("politician") or
        raw.get("Name") or raw.get("name") or raw.get("Member") or raw.get("member") or ""
    )
    chamber_raw = str(raw.get("Chamber") or raw.get("chamber") or "")
    chamber = "Senate" if "senate" in chamber_raw.lower() else "House" if "house" in chamber_raw.lower() else "Congress"
    tx_date = (
        raw.get("TransactionDate") or raw.get("transactionDate") or raw.get("transaction_date") or
        raw.get("Date") or raw.get("date") or ""
    )
    filing_date = (
        raw.get("ReportDate") or raw.get("reportDate") or raw.get("DisclosureDate") or
        raw.get("disclosure_date") or raw.get("FilingDate") or raw.get("filingDate") or ""
    )
    amount_raw = (
        raw.get("Amount") or raw.get("amount") or raw.get("Range") or
        raw.get("amount_range") or raw.get("AmountRange") or ""
    )
    return {
        "chamber":          chamber,
        "member":           str(member).strip(),
        "state":            raw.get("State") or raw.get("state") or "",
        "party":            str(raw.get("Party") or raw.get("party") or "")[:1].upper(),
        "ticker":           ticker,
        "asset":            raw.get("Asset") or raw.get("asset") or raw.get("AssetDescription") or raw.get("asset_description") or "",
        "asset_type":       raw.get("AssetType") or raw.get("asset_type") or "",
        "action":           _parse_action(raw.get("Transaction") or raw.get("transaction") or raw.get("TransactionType") or raw.get("trade_type") or ""),
        "amount_raw":       str(amount_raw),
        "amount_label":     _AMOUNT_LABEL.get(str(amount_raw), str(amount_raw) or "?"),
        "amount_order":     _AMOUNT_ORDER.get(str(amount_raw), 0),
        "transaction_date": _parse_date(str(tx_date)),
        "disclosure_date":  _parse_date(str(filing_date)),
        "comment":          raw.get("Comment") or raw.get("comment") or "",
    }


def _normalize_finnhub(raw: dict) -> dict:
    amount_from = raw.get("amountFrom")
    amount_to = raw.get("amountTo")
    if amount_from is not None and amount_to is not None:
        amount_raw = f"${float(amount_from):,.0f} - ${float(amount_to):,.0f}"
    else:
        amount_raw = str(raw.get("amount") or raw.get("amountRange") or "")
    return {
        "chamber":          "Congress",
        "member":           str(raw.get("name") or raw.get("representative") or "").strip(),
        "state":            "",
        "party":            "",
        "ticker":           _extract_ticker(raw.get("symbol") or raw.get("ticker") or ""),
        "asset":            raw.get("assetName") or raw.get("asset") or "",
        "asset_type":       "",
        "action":           _parse_action(raw.get("transactionType") or raw.get("transaction") or ""),
        "amount_raw":       amount_raw,
        "amount_label":     amount_raw or "?",
        "amount_order":     0,
        "transaction_date": _parse_date(raw.get("transactionDate") or ""),
        "disclosure_date":  _parse_date(raw.get("filingDate") or raw.get("disclosureDate") or ""),
        "comment":          raw.get("ownerType") or raw.get("position") or "",
    }


def _normalize_congressflow(raw: dict) -> dict:
    member_raw = str(raw.get("Politician") or raw.get("politician") or "").strip()
    party = member_raw[-1:] if member_raw[-1:] in ("D", "R", "I") else ""
    member = member_raw[:-1].strip() if party else member_raw
    member = re.sub(r"^[A-Z]{2,3}(?=[A-Z][a-z])", "", member).strip()
    amount_raw = str(raw.get("Amount") or raw.get("amount") or "")
    return {
        "chamber":          "Congress",
        "member":           member,
        "state":            "",
        "party":            party,
        "ticker":           _extract_ticker(str(raw.get("Ticker") or raw.get("ticker") or "")),
        "asset":            "",
        "asset_type":       "Stock",
        "action":           _parse_action(str(raw.get("Type") or raw.get("type") or "")),
        "amount_raw":       amount_raw,
        "amount_label":     _AMOUNT_LABEL.get(amount_raw, amount_raw or "?"),
        "amount_order":     _AMOUNT_ORDER.get(amount_raw, 0),
        "transaction_date": _parse_date(str(raw.get("Traded") or raw.get("traded") or "")),
        "disclosure_date":  _parse_date(str(raw.get("Filed") or raw.get("filed") or "")),
        "comment":          "congressflow",
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

            print(f"[Congress] {url} -> {len(normalized)} trades with tickers (from {len(rows)} raw rows)")
            return normalized, url

        except Exception as e:
            last_err = str(e)
            print(f"[Congress] {url} failed: {e}")
            continue

    print(f"[Congress] All endpoints failed. Last error: {last_err}")
    return [], ""


def _fetch_quiver(limit: int = 2000) -> list[dict]:
    key = _env("QUIVER_API_KEY") or _env("QUIVER_QUANT_API_KEY")
    if not key:
        return []
    headers = {**_HEADERS, "Authorization": f"Bearer {key}"}
    for url in _QUIVER_ENDPOINTS:
        try:
            resp = requests.get(url, headers=headers, params={"page_size": limit}, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("data") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                rows = []
            normalized = []
            for r in rows[:limit]:
                if not isinstance(r, dict):
                    continue
                try:
                    n = _normalize_quiver(r)
                    if n["ticker"]:
                        normalized.append(n)
                except Exception:
                    pass
            print(f"[Congress] Quiver {url} -> {len(normalized)} trades with tickers")
            if normalized:
                return normalized
        except Exception as e:
            print(f"[Congress] Quiver {url} failed: {e}")
    return []


def _fetch_congressflow(limit: int = 2000) -> list[dict]:
    try:
        import pandas as pd
        from io import StringIO
        resp = requests.get("https://congressflow.com/trades", headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        tables = pd.read_html(StringIO(resp.text))
        if not tables:
            return []
        rows = tables[0].head(limit).to_dict("records")
        normalized = []
        for r in rows:
            try:
                n = _normalize_congressflow(r)
                if n["ticker"]:
                    normalized.append(n)
            except Exception:
                pass
        print(f"[Congress] CongressFlow -> {len(normalized)} trades with tickers")
        return normalized
    except Exception as e:
        print(f"[Congress] CongressFlow failed: {type(e).__name__}: {e}")
        return []


def _fetch_house(limit: int = 2000) -> list[dict]:
    trades, _ = _fetch_from_endpoints(_HOUSE_ENDPOINTS, _normalize_house, limit)
    return trades


def _fetch_senate(limit: int = 2000) -> list[dict]:
    trades, _ = _fetch_from_endpoints(_SENATE_ENDPOINTS, _normalize_senate, limit)
    return trades


def _fetch_all(limit: int = 2000) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple] = set()

    for source_rows in (
        _fetch_quiver(limit),
        _fetch_congressflow(limit),
        _fetch_house(limit),
        _fetch_senate(limit),
    ):
        for t in source_rows:
            key = (
                t.get("ticker"),
                t.get("member"),
                t.get("action"),
                t.get("amount_raw"),
                t.get("transaction_date"),
                t.get("disclosure_date"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(t)

    return merged[:limit]


def _fetch_finnhub_ticker(ticker: str, days: int = 365) -> list[dict]:
    key = _env("FINNHUB_API_KEY")
    if not key:
        return []
    end = date.today()
    start = end - timedelta(days=days)
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/congressional-trading",
            params={
                "symbol": ticker.upper().strip(),
                "from": start.isoformat(),
                "to": end.isoformat(),
                "token": key,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("data") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            rows = []
        result = []
        for r in rows:
            if isinstance(r, dict):
                n = _normalize_finnhub(r)
                if n["ticker"]:
                    result.append(n)
        print(f"[Congress] Finnhub {ticker} -> {len(result)} trades")
        return result
    except Exception as e:
        print(f"[Congress] Finnhub {ticker} failed: {type(e).__name__}")
        return []


def _activity_date(t: dict) -> Optional[date]:
    return t.get("disclosure_date") or t.get("transaction_date")


# ── Debug helper ───────────────────────────────────────────────────────────────
def debug_raw(n: int = 5) -> dict:
    """
    Return raw API samples for debugging.
    Shows first N raw records from House and Senate before any normalisation.
    Call from !congressdebug command.
    """
    result = {
        "house": {"errors": []},
        "senate": {"errors": []},
        "quiver": {"configured": bool(_env("QUIVER_API_KEY") or _env("QUIVER_QUANT_API_KEY")), "errors": []},
        "finnhub": {"configured": bool(_env("FINNHUB_API_KEY")), "errors": []},
        "congressflow": {"errors": []},
    }

    key = _env("QUIVER_API_KEY") or _env("QUIVER_QUANT_API_KEY")
    if key:
        headers = {**_HEADERS, "Authorization": f"Bearer {key}"}
        for url in _QUIVER_ENDPOINTS:
            try:
                resp = requests.get(url, headers=headers, params={"page_size": n}, timeout=_TIMEOUT)
                result["quiver"]["status"] = resp.status_code
                result["quiver"]["url"] = url
                resp.raise_for_status()
                data = resp.json()
                rows = data.get("data") if isinstance(data, dict) else data
                rows = rows if isinstance(rows, list) else []
                result["quiver"].update({
                    "total_rows": len(rows),
                    "sample_keys": list(rows[0].keys()) if rows else [],
                    "samples": rows[:n],
                    "sample_tickers": list({str(r.get("Ticker") or r.get("ticker") or r.get("Symbol") or "").strip() for r in rows[:100]}),
                })
                if rows:
                    break
            except Exception as e:
                result["quiver"]["errors"].append(f"{url}: {e}")

    fh_key = _env("FINNHUB_API_KEY")
    if fh_key:
        try:
            end = date.today()
            start = end - timedelta(days=365)
            resp = requests.get(
                "https://finnhub.io/api/v1/stock/congressional-trading",
                params={"symbol": "NVDA", "from": start.isoformat(), "to": end.isoformat(), "token": fh_key},
                timeout=_TIMEOUT,
            )
            result["finnhub"]["status"] = resp.status_code
            result["finnhub"]["url"] = "https://finnhub.io/api/v1/stock/congressional-trading?symbol=NVDA"
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("data") if isinstance(data, dict) else data
            rows = rows if isinstance(rows, list) else []
            result["finnhub"].update({
                "total_rows": len(rows),
                "sample_keys": list(rows[0].keys()) if rows else [],
                "samples": rows[:n],
                "sample_tickers": list({str(r.get("symbol") or r.get("ticker") or "").strip() for r in rows[:100]}),
            })
        except Exception as e:
            result["finnhub"]["errors"].append(f"{type(e).__name__}: check FINNHUB_API_KEY / plan access")

    try:
        import pandas as pd
        from io import StringIO
        resp = requests.get("https://congressflow.com/trades", headers=_HEADERS, timeout=_TIMEOUT)
        result["congressflow"]["status"] = resp.status_code
        result["congressflow"]["url"] = "https://congressflow.com/trades"
        resp.raise_for_status()
        tables = pd.read_html(StringIO(resp.text))
        rows = tables[0].head(n).to_dict("records") if tables else []
        result["congressflow"].update({
            "total_rows": len(rows),
            "sample_keys": list(rows[0].keys()) if rows else [],
            "samples": rows[:n],
            "sample_tickers": list({str(r.get("Ticker") or "").strip() for r in rows[:100]}),
        })
    except Exception as e:
        result["congressflow"]["errors"].append(str(e))

    for url in _HOUSE_ENDPOINTS:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            result["house"]["status"] = resp.status_code
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
            result["house"]["errors"].append(f"{url}: {e}")

    for url in _SENATE_ENDPOINTS:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            result["senate"]["status"] = resp.status_code
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
            result["senate"]["errors"].append(f"{url}: {e}")

    return result


# ── Public API ─────────────────────────────────────────────────────────────────
def recent_trades(days: int = 30, actions: Optional[list[str]] = None) -> list[dict]:
    """All trades from the last `days` days, House + Senate, newest first."""
    cutoff = date.today() - timedelta(days=days)
    trades = _fetch_all()
    result = [
        t for t in trades
        if _activity_date(t) and _activity_date(t) >= cutoff
        and (actions is None or t["action"] in actions)
    ]
    result.sort(key=lambda x: _activity_date(x) or date.min, reverse=True)
    return result


def trades_for_ticker(ticker: str, days: int = 365) -> list[dict]:
    """All congressional trades for a specific ticker in the last `days` days."""
    ticker = ticker.upper().strip()
    cutoff = date.today() - timedelta(days=days)
    trades = _fetch_finnhub_ticker(ticker, days=days) + _fetch_all()
    result = [
        t for t in trades
        if t["ticker"] == ticker
        and _activity_date(t)
        and _activity_date(t) >= cutoff
    ]
    result.sort(key=lambda x: _activity_date(x) or date.min, reverse=True)
    return result


def watchlist_trades(tickers: list[str], days: int = 14) -> list[dict]:
    """Congressional trades for tickers in the given list, last `days` days."""
    universe = {t.upper() for t in tickers}
    cutoff   = date.today() - timedelta(days=days)
    trades   = _fetch_all()
    result   = [
        t for t in trades
        if t["ticker"] in universe
        and _activity_date(t)
        and _activity_date(t) >= cutoff
    ]
    result.sort(
        key=lambda x: (_activity_date(x) or date.min, x["amount_order"]),
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
        "congress_trades": sum(1 for t in trades if t["chamber"] == "Congress"),
        "most_active":   [{"member": m, "trades": c} for m, c in most_active],
    }
