# -*- coding: utf-8 -*-
"""
Conquest Trading — Automated Paper Trade Engine
=================================================
Generates 10 paper trades per day across all instrument types.
Marks to market daily and closes on profit/stop targets.
Run via APScheduler (web_app.py) or manually:
  python paper_trader.py          — generate today's trades
  python paper_trader.py close    — mark open trades + apply stops/targets
  python paper_trader.py stats    — print performance summary
"""

import json
import os
import random
import sys
from datetime import datetime, date
from typing import Optional

import pytz

APP_DIR   = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG = os.path.join(APP_DIR, "paper_trades.json")
ET        = pytz.timezone("America/New_York")

# ── Extended universe (20 liquid names for variety) ───────────────────────────
PAPER_UNIVERSE = [
    "AAPL", "NVDA", "MSFT", "GOOGL", "AMZN",
    "META", "TSLA", "JPM",  "XOM",   "WMT",
    "COP",  "SPY",  "QQQ",  "AMD",   "NFLX",
    "COST", "V",    "MA",   "BAC",   "DIS",
]

# ── Risk / sizing ──────────────────────────────────────────────────────────────
STOCK_SIZE       = 1_000   # $ notional per stock trade
OPTION_CONTRACTS = 1       # 1 contract = 100 shares
RISK_FREE        = 0.05    # 5 % annualised
DTE_TARGET       = 30      # days to expiry at entry
MAX_HOLD_DAYS    = 5       # close after 5 calendar days regardless

# ── Exit thresholds ────────────────────────────────────────────────────────────
OPT_PROFIT   =  0.50   # close option when up  50 %
OPT_STOP     = -0.75   # close option when down 75 %
STK_PROFIT   =  0.05   # close stock  when up   5 %
STK_STOP     = -0.03   # close stock  when down 3 %
IC_PROFIT    =  0.50   # close iron condor when 50 % of max credit earned
IC_STOP      =  2.00   # close iron condor when position costs 2× credit (loss)


# ── Persistence ───────────────────────────────────────────────────────────────

def load_trades() -> list:
    from db import kv_get
    data = kv_get("paper_trades")
    return data if isinstance(data, list) else []


def save_trades(trades: list) -> None:
    from db import kv_set
    kv_set("paper_trades", trades)


# ── Pricing helpers ────────────────────────────────────────────────────────────

def _bs(S: float, K: float, T: float, sigma: float, opt_type: str = "call") -> float:
    """Black-Scholes wrapper — returns 0 gracefully on bad inputs."""
    try:
        from models.black_scholes import black_scholes_price
        if T <= 0:
            return max(S - K, 0.0) if opt_type == "call" else max(K - S, 0.0)
        return float(black_scholes_price(S, K, T, RISK_FREE, sigma, opt_type))
    except Exception:
        return 0.0


def _hv(ticker: str, fallback: float = 0.28) -> float:
    """30-day historical annualised volatility."""
    try:
        import yfinance as yf
        import numpy as np
        df = yf.download(ticker, period="60d", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 10:
            return fallback
        rets = float(df["Close"].pct_change().dropna().std()) * (252 ** 0.5)
        return max(rets, 0.05)   # floor at 5 %
    except Exception:
        return fallback


def _live_price(ticker: str) -> Optional[float]:
    try:
        import yfinance as yf
        p = float(yf.Ticker(ticker).fast_info["lastPrice"])
        return p if p > 0 else None
    except Exception:
        return None


def _round_strike(price: float, step: float) -> float:
    return round(round(price / step) * step, 2)


def _strike_step(price: float) -> float:
    if price >= 200:   return 5.0
    if price >= 50:    return 2.5
    if price >= 20:    return 1.0
    return 0.5


# ── Trade-type assignment ──────────────────────────────────────────────────────

_TYPE_WEIGHTS = {
    "stock_long":   1,
    "stock_short":  1,
    "call_spread":  2,
    "put_spread":   2,
    "long_call":    1,
    "long_put":     1,
    "iron_condor":  2,
}

def _assign_trade_type(scan: dict) -> str:
    """
    Signal-driven trade-type selection.
    Directional signals → directional trades.
    High IV / neutral → iron condor or credit spread.
    """
    daily      = scan.get("daily",  "BULL")
    weekly     = scan.get("weekly", "BULL")
    rsi        = scan.get("rsi",    50.0)
    hv_rank    = scan.get("hv_rank", 0.30)
    sqz_fired  = scan.get("sqz_fired", False)
    sqz_mom    = scan.get("sqz_momentum", 0.0)
    adx        = scan.get("adx",   20.0)
    entry_sig  = scan.get("entry_signal", False)

    bullish  = (daily == "BULL") and (weekly == "BULL")
    bearish  = (daily == "BEAR") and (weekly == "BEAR")
    trending = adx > 22

    # High IV environment → prefer selling premium
    if hv_rank > 0.65 and not sqz_fired:
        if bullish and trending:
            return "put_spread"
        if bearish and trending:
            return "call_spread"
        return "iron_condor"

    if bullish and trending:
        if sqz_fired and sqz_mom > 0:
            return random.choice(["call_spread", "long_call", "call_spread"])
        if entry_sig and rsi < 45:
            return random.choice(["long_call", "stock_long"])
        return random.choice(["call_spread", "stock_long", "long_call", "call_spread"])

    if bearish and trending:
        if sqz_fired and sqz_mom < 0:
            return random.choice(["put_spread", "long_put", "put_spread"])
        if rsi > 58:
            return random.choice(["long_put", "stock_short"])
        return random.choice(["put_spread", "stock_short", "long_put", "put_spread"])

    # Neutral / mixed — range-bound candidates
    if hv_rank > 0.40:
        return "iron_condor"
    return random.choice(["iron_condor", "call_spread", "put_spread"])


# ── Trade builders ─────────────────────────────────────────────────────────────

def _build_stock(scan: dict, trade_type: str, ts: str) -> Optional[dict]:
    price = scan.get("price", 0)
    if not price:
        return None
    shares = round(STOCK_SIZE / price, 4)
    return {
        "id":           f"{ts}_{scan['ticker']}_{trade_type}",
        "date_entered": ts,
        "ticker":       scan["ticker"],
        "trade_type":   trade_type,
        "status":       "open",
        "side":         "long" if trade_type == "stock_long" else "short",
        "entry_price":  round(price, 4),
        "shares":       shares,
        "cost_basis":   round(price * shares, 2),
        "current_price": round(price, 4),
        "pnl":          0.0,
        "pnl_pct":      0.0,
        "date_closed":  None,
        "close_reason": None,
        "days_held":    0,
        # signal context (for review)
        "mtf_score":    scan.get("mtf_score", 0),
        "rsi_entry":    round(scan.get("rsi", 50), 1),
    }


def _build_option(scan: dict, trade_type: str, ts: str) -> Optional[dict]:
    price = scan.get("price", 0)
    if not price:
        return None
    sigma  = _hv(scan["ticker"])
    T      = DTE_TARGET / 365.0
    step   = _strike_step(price)

    if trade_type == "long_call":
        strike   = _round_strike(price * 1.02, step)
        opt_type = "call"
    else:
        strike   = _round_strike(price * 0.98, step)
        opt_type = "put"

    val = _bs(price, strike, T, sigma, opt_type)
    if val <= 0.05:
        return None

    cost = round(val * 100 * OPTION_CONTRACTS, 2)
    return {
        "id":                  f"{ts}_{scan['ticker']}_{trade_type}",
        "date_entered":        ts,
        "ticker":              scan["ticker"],
        "trade_type":          trade_type,
        "status":              "open",
        "opt_type":            opt_type,
        "strike":              strike,
        "t_days":              DTE_TARGET,
        "sigma":               round(sigma, 4),
        "entry_stock_price":   round(price, 4),
        "entry_option_price":  round(val, 4),
        "cost_basis":          cost,
        "contracts":           OPTION_CONTRACTS,
        "current_option_price": round(val, 4),
        "pnl":                 0.0,
        "pnl_pct":             0.0,
        "date_closed":         None,
        "close_reason":        None,
        "days_held":           0,
        "mtf_score":           scan.get("mtf_score", 0),
        "rsi_entry":           round(scan.get("rsi", 50), 1),
    }


def _build_spread(scan: dict, trade_type: str, ts: str) -> Optional[dict]:
    price = scan.get("price", 0)
    if not price:
        return None
    sigma    = _hv(scan["ticker"])
    T        = DTE_TARGET / 365.0
    step     = _strike_step(price)
    width    = step * 2   # e.g. 5-wide on $200 stock, 2-wide on $60 stock

    if trade_type == "call_spread":
        long_k   = _round_strike(price * 1.01, step)
        short_k  = long_k + width
        opt_type = "call"
    else:  # put_spread
        long_k   = _round_strike(price * 0.99, step)
        short_k  = long_k - width
        opt_type = "put"

    long_val  = _bs(price, long_k,  T, sigma, opt_type)
    short_val = _bs(price, short_k, T, sigma, opt_type)
    debit     = round(long_val - short_val, 4)

    if debit <= 0.05:
        return None

    max_gain = round(width - debit, 4)
    cost     = round(debit * 100 * OPTION_CONTRACTS, 2)

    return {
        "id":                f"{ts}_{scan['ticker']}_{trade_type}",
        "date_entered":      ts,
        "ticker":            scan["ticker"],
        "trade_type":        trade_type,
        "status":            "open",
        "opt_type":          opt_type,
        "long_strike":       long_k,
        "short_strike":      short_k,
        "spread_width":      width,
        "t_days":            DTE_TARGET,
        "sigma":             round(sigma, 4),
        "entry_stock_price": round(price, 4),
        "entry_net_debit":   debit,
        "max_gain":          max_gain,
        "cost_basis":        cost,
        "contracts":         OPTION_CONTRACTS,
        "current_net_value": debit,
        "pnl":               0.0,
        "pnl_pct":           0.0,
        "date_closed":       None,
        "close_reason":      None,
        "days_held":         0,
        "mtf_score":         scan.get("mtf_score", 0),
        "rsi_entry":         round(scan.get("rsi", 50), 1),
    }


def _build_iron_condor(scan: dict, ts: str) -> Optional[dict]:
    price = scan.get("price", 0)
    if not price:
        return None
    sigma    = _hv(scan["ticker"])
    T        = DTE_TARGET / 365.0
    step     = _strike_step(price)
    width    = step * 2

    # Short strangle ≈10 % OTM, long wings one width further out
    sc_k = _round_strike(price * 1.08, step)
    lc_k = sc_k + width
    sp_k = _round_strike(price * 0.92, step)
    lp_k = sp_k - width

    sc = _bs(price, sc_k, T, sigma, "call")
    lc = _bs(price, lc_k, T, sigma, "call")
    sp = _bs(price, sp_k, T, sigma, "put")
    lp = _bs(price, lp_k, T, sigma, "put")

    credit = round((sc - lc) + (sp - lp), 4)
    if credit <= 0.05:
        return None

    max_loss = round(width - credit, 4)

    return {
        "id":                f"{ts}_{scan['ticker']}_iron_condor",
        "date_entered":      ts,
        "ticker":            scan["ticker"],
        "trade_type":        "iron_condor",
        "status":            "open",
        "long_call_k":       lc_k,
        "short_call_k":      sc_k,
        "short_put_k":       sp_k,
        "long_put_k":        lp_k,
        "spread_width":      width,
        "t_days":            DTE_TARGET,
        "sigma":             round(sigma, 4),
        "entry_stock_price": round(price, 4),
        "entry_net_credit":  credit,
        "max_gain":          credit,
        "max_loss":          max_loss,
        "cost_basis":        round(max_loss * 100 * OPTION_CONTRACTS, 2),  # max risk
        "contracts":         OPTION_CONTRACTS,
        "current_net_value": credit,
        "pnl":               0.0,
        "pnl_pct":           0.0,
        "date_closed":       None,
        "close_reason":      None,
        "days_held":         0,
        "mtf_score":         scan.get("mtf_score", 0),
        "rsi_entry":         round(scan.get("rsi", 50), 1),
    }


def _build_trade(scan: dict, trade_type: str, ts: str) -> Optional[dict]:
    if trade_type in ("stock_long", "stock_short"):
        return _build_stock(scan, trade_type, ts)
    if trade_type in ("long_call", "long_put"):
        return _build_option(scan, trade_type, ts)
    if trade_type in ("call_spread", "put_spread"):
        return _build_spread(scan, trade_type, ts)
    if trade_type == "iron_condor":
        return _build_iron_condor(scan, ts)
    return None


# ── Mark-to-market ─────────────────────────────────────────────────────────────

def _days_since(date_str: str) -> int:
    try:
        entry = datetime.fromisoformat(date_str[:19]).date()
        return max((date.today() - entry).days, 0)
    except Exception:
        return 0


def mark_trade(trade: dict, price: Optional[float] = None) -> dict:
    """Re-price an open trade. Pass price to avoid redundant yfinance calls."""
    if trade["status"] != "open":
        return trade

    t = dict(trade)
    if price is None:
        price = _live_price(t["ticker"])
    if not price:
        return t

    tt        = t["trade_type"]
    days_held = _days_since(t["date_entered"])
    t["days_held"] = days_held

    try:
        if tt in ("stock_long", "stock_short"):
            sign = 1 if tt == "stock_long" else -1
            t["current_price"] = round(price, 4)
            raw  = (price - t["entry_price"]) * t["shares"] * sign
            t["pnl"]     = round(raw, 2)
            t["pnl_pct"] = round(raw / t["cost_basis"], 4) if t["cost_basis"] else 0.0

        elif tt in ("long_call", "long_put"):
            T_rem = max((t["t_days"] - days_held) / 365.0, 0.001)
            cur   = _bs(price, t["strike"], T_rem, t["sigma"], t["opt_type"])
            t["current_option_price"] = round(cur, 4)
            raw  = (cur - t["entry_option_price"]) * 100 * t["contracts"]
            t["pnl"]     = round(raw, 2)
            t["pnl_pct"] = round(raw / t["cost_basis"], 4) if t["cost_basis"] else 0.0

        elif tt in ("call_spread", "put_spread"):
            T_rem  = max((t["t_days"] - days_held) / 365.0, 0.001)
            lv     = _bs(price, t["long_strike"],  T_rem, t["sigma"], t["opt_type"])
            sv     = _bs(price, t["short_strike"], T_rem, t["sigma"], t["opt_type"])
            net    = lv - sv
            t["current_net_value"] = round(net, 4)
            raw  = (net - t["entry_net_debit"]) * 100 * t["contracts"]
            t["pnl"]     = round(raw, 2)
            t["pnl_pct"] = round(raw / t["cost_basis"], 4) if t["cost_basis"] else 0.0

        elif tt == "iron_condor":
            T_rem = max((t["t_days"] - days_held) / 365.0, 0.001)
            sc = _bs(price, t["short_call_k"], T_rem, t["sigma"], "call")
            lc = _bs(price, t["long_call_k"],  T_rem, t["sigma"], "call")
            sp = _bs(price, t["short_put_k"],  T_rem, t["sigma"], "put")
            lp = _bs(price, t["long_put_k"],   T_rem, t["sigma"], "put")
            cur_net = (sc - lc) + (sp - lp)
            t["current_net_value"] = round(cur_net, 4)
            # credit trade: profit = original credit − current cost to close
            raw  = (t["entry_net_credit"] - cur_net) * 100 * t["contracts"]
            ml   = t.get("max_loss", 1) * 100 or 100
            t["pnl"]     = round(raw, 2)
            t["pnl_pct"] = round(raw / ml, 4)

    except Exception:
        pass

    return t


def _should_close(t: dict) -> Optional[str]:
    days = t.get("days_held", 0)
    pct  = t.get("pnl_pct",  0.0)
    pnl  = t.get("pnl",      0.0)
    tt   = t["trade_type"]

    if days >= MAX_HOLD_DAYS:
        return "max_hold"

    if tt in ("stock_long", "stock_short"):
        if pct >= STK_PROFIT:  return "profit_target"
        if pct <= STK_STOP:    return "stop_loss"

    elif tt in ("long_call", "long_put", "call_spread", "put_spread"):
        if pct >= OPT_PROFIT:  return "profit_target"
        if pct <= OPT_STOP:    return "stop_loss"

    elif tt == "iron_condor":
        credit_usd = t.get("entry_net_credit", 0) * 100
        if credit_usd > 0:
            if pnl >= credit_usd * IC_PROFIT:  return "profit_target"
            if pnl <= -credit_usd * IC_STOP:   return "stop_loss"

    return None


# ── Daily generate ─────────────────────────────────────────────────────────────

def generate_daily_trades(n: int = 10) -> list:
    """
    Generate n paper trades using the ConquestAgentSystem (6 parallel AI agents).
    Falls back to signal-based generation for any slots the agent system can't fill.
    Safe to call multiple times — skips if today's batch already exists.
    Returns list of new trades added (empty if already ran today).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    sys.path.insert(0, APP_DIR)

    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    all_trades = load_trades()

    # Already ran today?
    already_today = [t for t in all_trades
                     if t.get("date_entered", "").startswith(today_str)]
    if len(already_today) >= n:
        print(f"[PaperTrader] Already have {len(already_today)} trades for {today_str}.")
        return []

    used_tickers = {t["ticker"] for t in already_today}
    ts           = datetime.now(ET).strftime("%Y-%m-%dT%H:%M")
    new_trades   = []
    type_counts  = {tt: 0 for tt in _TYPE_WEIGHTS}

    # ── Pre-screen: expand from 20 to 120-ticker universe, pick top 40 ──────────
    try:
        from universe_screener import pre_screen
        scan_universe = pre_screen(n=40)
    except Exception as _ps_err:
        print(f"[PaperTrader] Pre-screener unavailable ({_ps_err}), using base universe.")
        scan_universe = PAPER_UNIVERSE

    # ── Primary path: ConquestAgentSystem (6-agent swarm) ─────────────────────
    try:
        from conquest_agents import get_agent_system
        print(f"[PaperTrader] Launching 6-agent swarm across {len(scan_universe)} candidates …")
        agent_trades = get_agent_system().generate_trades(
            scan_universe, n=n, existing_tickers=used_tickers
        )
        new_trades   = agent_trades
        for t in new_trades:
            type_counts[t["trade_type"]] = type_counts.get(t["trade_type"], 0) + 1
            used_tickers.add(t["ticker"])
        print(f"[PaperTrader] Agent system produced {len(new_trades)} high-conviction trades.")
    except Exception as e:
        print(f"[PaperTrader] Agent system unavailable ({e}), using signal scanner.")
        new_trades = []

    # ── Fallback / fill: signal-based for any remaining slots ─────────────────
    if len(new_trades) < n:
        remaining_needed = n - len(new_trades)
        print(f"[PaperTrader] Filling {remaining_needed} slot(s) via signal scanner …")
        from alerts.scanner import scan_ticker

        scans = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(scan_ticker, t): t
                    for t in scan_universe if t not in used_tickers}
            for fut in as_completed(futs):
                r = fut.result()
                if not r.get("error") and (r.get("price") or 0) > 0:
                    scans.append(r)

        scans.sort(key=lambda s: (
            -int(s.get("sqz_fired",    False)),
            -int(s.get("entry_signal", False)),
            -s.get("mtf_score", 0),
        ))

        # First pass: top-signal tickers
        for scan in scans:
            if len(new_trades) >= n:
                break
            if scan["ticker"] in used_tickers:
                continue

            trade_type = _assign_trade_type(scan)

            if type_counts.get(trade_type, 0) >= 3:
                pool_candidates = [tt for tt in _TYPE_WEIGHTS
                                   if type_counts.get(tt, 0) < 3]
                if not pool_candidates:
                    break
                trade_type = min(pool_candidates,
                                 key=lambda tt: type_counts.get(tt, 0))

            trade = _build_trade(scan, trade_type, ts)
            if trade:
                trade["adx_entry"] = round(scan.get("adx", 0), 1)
                new_trades.append(trade)
                type_counts[trade_type] = type_counts.get(trade_type, 0) + 1
                used_tickers.add(scan["ticker"])

        # Second pass: iron condors on leftovers
        if len(new_trades) < n:
            remaining = [s for s in scans if s["ticker"] not in used_tickers]
            fallback_types = ["iron_condor", "call_spread", "put_spread",
                              "long_call", "long_put", "stock_long"]
            fi = 0
            for scan in remaining:
                if len(new_trades) >= n:
                    break
                tt    = fallback_types[fi % len(fallback_types)]
                fi   += 1
                trade = _build_trade(scan, tt, ts)
                if trade:
                    new_trades.append(trade)
                    used_tickers.add(scan["ticker"])

    # ── Generate entry reasoning for every new trade ──────────────────────────
    if new_trades:
        try:
            from conquest_brain import generate_trade_reasonings
            # Match each trade back to its scan result
            scan_map = {s["ticker"]: s for s in scans}
            trades_and_scans = [
                (t, scan_map.get(t["ticker"], {}))
                for t in new_trades
            ]
            reasonings = generate_trade_reasonings(trades_and_scans)
            for t in new_trades:
                t["reasoning"] = reasonings.get(t["id"], "Reasoning unavailable.")
            print(f"[PaperTrader] Entry reasoning generated for {len(reasonings)} trades.")
        except Exception as e:
            print(f"[PaperTrader] Reasoning generation failed: {e}")
            for t in new_trades:
                t.setdefault("reasoning", "Reasoning unavailable.")

    all_trades.extend(new_trades)
    save_trades(all_trades)

    # Log each new trade to Notion Trade Journal
    try:
        from notion_journal import log_trade_open
        logged = sum(1 for t in new_trades if log_trade_open(t))
        if logged:
            print(f"[PaperTrader] Logged {logged} trades to Notion.")
    except Exception as e:
        print(f"[PaperTrader] Notion open-logging skipped: {e}")

    summary = {}
    for t in new_trades:
        summary[t["trade_type"]] = summary.get(t["trade_type"], 0) + 1

    print(f"[PaperTrader] Generated {len(new_trades)} trades: {summary}")
    return new_trades


# ── Daily close ────────────────────────────────────────────────────────────────

def run_daily_close() -> dict:
    """
    Mark every open trade to market, close any that hit stop/target/max-hold.
    Call at market close (4:05 PM ET).
    """
    trades = load_trades()
    open_trades = [t for t in trades if t.get("status") == "open"]

    # Batch price fetch — one yfinance call per unique ticker
    import yfinance as yf
    tickers = list({t["ticker"] for t in open_trades})
    prices  = {}
    if tickers:
        try:
            data = yf.download(tickers, period="1d", interval="1m",
                               progress=False, auto_adjust=True)
            for tk in tickers:
                try:
                    prices[tk] = float(yf.Ticker(tk).fast_info["lastPrice"])
                except Exception:
                    pass
        except Exception:
            pass

    closed_count = 0
    now_str      = datetime.now(ET).strftime("%Y-%m-%dT%H:%M")

    for i, trade in enumerate(trades):
        if trade["status"] != "open":
            continue
        price        = prices.get(trade["ticker"])
        trades[i]    = mark_trade(trade, price=price)
        reason       = _should_close(trades[i])
        if reason:
            trades[i]["status"]       = "closed"
            trades[i]["date_closed"]  = now_str
            trades[i]["close_reason"] = reason
            # Generate close reasoning
            try:
                from conquest_brain import generate_close_reasoning
                trades[i]["close_reasoning"] = generate_close_reasoning(
                    trades[i], reason, price or 0
                )
            except Exception:
                trades[i]["close_reasoning"] = "Reasoning unavailable."
            # Update agent weights so the swarm learns from this outcome
            try:
                from conquest_agents import get_agent_system
                get_agent_system().update_weights_from_trade(trades[i])
            except Exception as _aw_err:
                pass   # weight learning is best-effort
            closed_count += 1

    save_trades(trades)

    # Log all closed trades to Notion
    try:
        from notion_journal import log_trade_close
        closed_trades = [t for t in trades if t.get("status") == "closed"
                         and t.get("date_closed", "").startswith(now_str[:10])]
        for t in closed_trades:
            log_trade_close(t)
        if closed_trades:
            print(f"[PaperTrader] Notion close-log: {len(closed_trades)} trades updated.")
    except Exception as e:
        print(f"[PaperTrader] Notion close-logging skipped: {e}")
    result = {
        "total_open":  len(open_trades),
        "closed":      closed_count,
        "still_open":  len(open_trades) - closed_count,
    }
    print(f"[PaperTrader] Close run: {result}")
    return result


# ── Statistics ─────────────────────────────────────────────────────────────────

def get_paper_stats() -> dict:
    """Return a comprehensive stats dict for the web dashboard and Discord."""
    trades  = load_trades()
    closed  = [t for t in trades if t.get("status") == "closed"]
    open_   = [t for t in trades if t.get("status") == "open"]

    base = {
        "total_trades": len(trades),
        "open_count":   len(open_),
        "closed_count": len(closed),
        "open_trades":  sorted(open_,   key=lambda t: t.get("pnl", 0), reverse=True),
        "closed_trades": sorted(closed, key=lambda t: t.get("date_closed") or "", reverse=True),
    }

    if not closed:
        return {**base,
                "win_rate": 0, "total_pnl": 0, "avg_pnl": 0,
                "avg_hold": 0, "best_trade": None, "worst_trade": None,
                "by_type": {}, "by_ticker": {},
                "sharpe": None, "profit_factor": None, "cum_pnl": []}

    wins      = [t for t in closed if t.get("pnl", 0) > 0]
    total_pnl = round(sum(t.get("pnl", 0) for t in closed), 2)
    avg_pnl   = round(total_pnl / len(closed), 2)
    avg_hold  = round(sum(t.get("days_held", 0) for t in closed) / len(closed), 1)
    best      = max(closed, key=lambda t: t.get("pnl", 0))
    worst     = min(closed, key=lambda t: t.get("pnl", 0))

    # ── By trade type ──────────────────────────────────────────────────────────
    by_type: dict = {}
    for t in closed:
        tt = t["trade_type"]
        if tt not in by_type:
            by_type[tt] = {"count": 0, "wins": 0, "total_pnl": 0.0}
        by_type[tt]["count"]     += 1
        by_type[tt]["total_pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            by_type[tt]["wins"] += 1
    for tt, d in by_type.items():
        d["win_rate"] = round(d["wins"] / d["count"], 3)
        d["avg_pnl"]  = round(d["total_pnl"] / d["count"], 2)
        d["total_pnl"] = round(d["total_pnl"], 2)

    # ── By ticker ──────────────────────────────────────────────────────────────
    by_ticker: dict = {}
    for t in closed:
        tk = t["ticker"]
        if tk not in by_ticker:
            by_ticker[tk] = {"count": 0, "wins": 0, "total_pnl": 0.0}
        by_ticker[tk]["count"]     += 1
        by_ticker[tk]["total_pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            by_ticker[tk]["wins"] += 1
    for tk, d in by_ticker.items():
        d["win_rate"]   = round(d["wins"] / d["count"], 3)
        d["avg_pnl"]    = round(d["total_pnl"] / d["count"], 2)
        d["total_pnl"]  = round(d["total_pnl"], 2)

    # ── Sharpe (annualised from per-trade P&L) ────────────────────────────────
    sharpe = None
    try:
        import numpy as np
        from collections import defaultdict
        daily_pnl: dict = defaultdict(float)
        for t in closed:
            d = (t.get("date_closed") or t.get("date_entered", ""))[:10]
            daily_pnl[d] += t.get("pnl", 0)
        vals = list(daily_pnl.values())
        if len(vals) > 1:
            mean_r = float(np.mean(vals))
            std_r  = float(np.std(vals, ddof=1))
            sharpe = round(mean_r / std_r * (252 ** 0.5), 2) if std_r > 0 else 0.0
    except Exception:
        pass

    # ── Profit factor ──────────────────────────────────────────────────────────
    gross_win  = sum(t.get("pnl", 0) for t in closed if t.get("pnl", 0) > 0)
    gross_loss = abs(sum(t.get("pnl", 0) for t in closed if t.get("pnl", 0) < 0))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    # ── Cumulative P&L curve ───────────────────────────────────────────────────
    sorted_closed = sorted(closed,
                           key=lambda t: t.get("date_closed") or t.get("date_entered", ""))
    cum_pnl, running = [], 0.0
    for t in sorted_closed:
        running += t.get("pnl", 0)
        cum_pnl.append({
            "date":    (t.get("date_closed") or t.get("date_entered", ""))[:10],
            "cum_pnl": round(running, 2),
            "ticker":  t["ticker"],
        })

    # ── Close-reason breakdown ─────────────────────────────────────────────────
    reason_counts: dict = {}
    for t in closed:
        r = t.get("close_reason", "unknown")
        reason_counts[r] = reason_counts.get(r, 0) + 1

    return {
        **base,
        "win_rate":      round(len(wins) / len(closed), 3),
        "total_pnl":     total_pnl,
        "avg_pnl":       avg_pnl,
        "avg_hold":      avg_hold,
        "best_trade":    best,
        "worst_trade":   worst,
        "by_type":       by_type,
        "by_ticker":     by_ticker,
        "sharpe":        sharpe,
        "profit_factor": profit_factor,
        "cum_pnl":       cum_pnl,
        "reason_counts": reason_counts,
    }


# ── CLI entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.path.insert(0, APP_DIR)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "generate"

    if cmd == "generate":
        trades = generate_daily_trades(10)
        if trades:
            print(f"\nNew trades:")
            for t in trades:
                print(f"  {t['trade_type']:15s}  {t['ticker']:6s}  cost=${t.get('cost_basis',0):.2f}")
        else:
            print("No new trades added (already ran today or no data).")

    elif cmd == "close":
        result = run_daily_close()
        print(f"\nClose result: {result}")

    elif cmd == "stats":
        s = get_paper_stats()
        print(f"\n{'='*50}")
        print(f"  PAPER TRADING STATS")
        print(f"{'='*50}")
        print(f"  Total trades : {s['total_trades']}  (open: {s['open_count']}, closed: {s['closed_count']})")
        if s["closed_count"]:
            print(f"  Win rate     : {s['win_rate']*100:.1f}%")
            print(f"  Total P&L    : ${s['total_pnl']:.2f}")
            print(f"  Avg P&L/trade: ${s['avg_pnl']:.2f}")
            print(f"  Sharpe       : {s['sharpe']}")
            print(f"  Profit factor: {s['profit_factor']}")
            print(f"\n  By type:")
            for tt, d in s["by_type"].items():
                print(f"    {tt:15s}  {d['count']:3d} trades  {d['win_rate']*100:5.1f}% win  avg ${d['avg_pnl']:+.2f}")

    else:
        print(f"Unknown command: {cmd}. Use: generate | close | stats")
