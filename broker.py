# -*- coding: utf-8 -*-
"""
Conquest Trading — Broker Execution Layer
==========================================
Wraps Alpaca Markets API for automated order execution.
Currently wired for PAPER TRADING — flip ALPACA_PAPER=false in Railway env
vars when you're ready for real money (after 2-3 months of data collection).

Supports:
  - Stock orders (market, limit) via Alpaca
  - Options orders: simulated via paper_trader.py (Alpaca doesn't do options)
    → TastyTrade integration planned for Phase 2

Architecture:
  paper_trader.generate_daily_trades()
       ↓
  broker.execute_trade(trade)          ← this file
       ↓
  Alpaca API (paper) or simulation     ← depending on ALPACA_PAPER env var

Environment variables (set in Railway):
  ALPACA_API_KEY      — from alpaca.markets dashboard
  ALPACA_SECRET_KEY   — from alpaca.markets dashboard
  ALPACA_PAPER        — "true" (default) | "false" for live
"""

import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER  = os.getenv("ALPACA_PAPER", "true").lower() != "false"

# Alpaca base URLs
_PAPER_URL = "https://paper-api.alpaca.markets"
_LIVE_URL  = "https://api.alpaca.markets"


# ── Client factory ─────────────────────────────────────────────────────────────

def _get_alpaca():
    """Return an authenticated Alpaca TradingClient. Raises if keys missing."""
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise RuntimeError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in Railway env vars. "
            "Get them at alpaca.markets → Your Account → API Keys."
        )
    from alpaca.trading.client import TradingClient
    return TradingClient(
        api_key=ALPACA_KEY,
        secret_key=ALPACA_SECRET,
        paper=ALPACA_PAPER,
    )


def broker_available() -> bool:
    """True if Alpaca keys are configured."""
    return bool(ALPACA_KEY and ALPACA_SECRET)


def get_account() -> dict:
    """Return account info: buying power, portfolio value, etc."""
    try:
        client  = _get_alpaca()
        account = client.get_account()
        return {
            "status":          str(account.status),
            "buying_power":    float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
            "cash":            float(account.cash),
            "paper":           ALPACA_PAPER,
            "mode":            "PAPER" if ALPACA_PAPER else "LIVE",
        }
    except Exception as e:
        return {"error": str(e)}


# ── Order execution ────────────────────────────────────────────────────────────

def execute_trade(trade: dict) -> dict:
    """
    Submit a trade dict (from paper_trader or agent system) to Alpaca.

    Stock longs → market order via Alpaca API
    Options (long_call, long_put, spreads, iron_condor) → simulated locally
      (Alpaca does not support options; TastyTrade integration is Phase 2)

    Returns enriched trade dict with broker_order_id, broker_status, fill_price.
    """
    result = dict(trade)
    tt     = trade.get("trade_type", "")

    # ── Options: stay simulated ───────────────────────────────────────────────
    if tt in ("long_call", "long_put", "call_spread", "put_spread", "iron_condor"):
        result["broker_status"] = "simulated"
        result["broker_note"]   = "Options simulated locally (Alpaca stocks only). Phase 2: TastyTrade."
        return result

    # ── Stocks: submit to Alpaca ───────────────────────────────────────────────
    if tt in ("stock_long", "stock_short"):
        if not broker_available():
            result["broker_status"] = "simulated"
            result["broker_note"]   = "No Alpaca keys — add ALPACA_API_KEY + ALPACA_SECRET_KEY to Railway."
            return result

        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums    import OrderSide, TimeInForce

            ticker  = trade["ticker"]
            side    = OrderSide.BUY if tt == "stock_long" else OrderSide.SELL
            shares  = round(trade.get("shares", 1), 0)
            if shares <= 0:
                shares = 1

            client = _get_alpaca()
            req    = MarketOrderRequest(
                symbol       = ticker,
                qty          = shares,
                side         = side,
                time_in_force= TimeInForce.DAY,
            )
            order = client.submit_order(req)

            result["broker_order_id"] = str(order.id)
            result["broker_status"]   = str(order.status)
            result["broker_mode"]     = "PAPER" if ALPACA_PAPER else "LIVE"
            result["broker_note"]     = f"Alpaca {'paper' if ALPACA_PAPER else 'LIVE'} order submitted"
            print(f"[Broker] {ticker} {side.value} {shares}sh → Alpaca order {order.id} ({order.status})")

        except Exception as e:
            result["broker_status"] = "failed"
            result["broker_note"]   = str(e)[:200]
            print(f"[Broker] {trade['ticker']} order FAILED: {e}")

    return result


def close_position(trade: dict, reason: str = "system") -> dict:
    """
    Close a stock position on Alpaca. Options are closed in simulation.
    Called by paper_trader.run_daily_close() when a stop/target/max-hold triggers.
    """
    result = dict(trade)
    tt     = trade.get("trade_type", "")

    if tt in ("long_call", "long_put", "call_spread", "put_spread", "iron_condor"):
        result["broker_close_status"] = "simulated"
        return result

    if not broker_available():
        result["broker_close_status"] = "simulated"
        return result

    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums    import OrderSide, TimeInForce

        ticker = trade["ticker"]
        shares = round(trade.get("shares", 1), 0)
        # Closing: sell for long, buy-to-cover for short
        side   = (OrderSide.SELL if trade.get("trade_type") == "stock_long"
                  else OrderSide.BUY)

        client = _get_alpaca()
        req    = MarketOrderRequest(
            symbol=ticker, qty=shares, side=side,
            time_in_force=TimeInForce.DAY,
        )
        order = client.submit_order(req)

        result["broker_close_id"]     = str(order.id)
        result["broker_close_status"] = str(order.status)
        result["broker_close_reason"] = reason
        print(f"[Broker] CLOSE {ticker} {side.value} {shares}sh — reason: {reason} → {order.id}")

    except Exception as e:
        result["broker_close_status"] = f"failed: {e}"[:100]
        print(f"[Broker] CLOSE FAILED {trade['ticker']}: {e}")

    return result


def get_open_positions() -> list:
    """Fetch open positions from Alpaca account."""
    if not broker_available():
        return []
    try:
        client    = _get_alpaca()
        positions = client.get_all_positions()
        return [
            {
                "ticker":     p.symbol,
                "qty":        float(p.qty),
                "avg_entry":  float(p.avg_entry_price),
                "market_val": float(p.market_value),
                "unrealized_pnl":     float(p.unrealized_pl),
                "unrealized_pnl_pct": float(p.unrealized_plpc) * 100,
                "side":       str(p.side),
            }
            for p in positions
        ]
    except Exception as e:
        print(f"[Broker] get_open_positions error: {e}")
        return []


# ── Intraday stop monitor ──────────────────────────────────────────────────────

def check_intraday_stops() -> list:
    """
    Check all open paper trades against current prices.
    Closes any that have hit stop_loss or profit_target intraday.
    Called every 30 min during market hours by the scheduler.
    Returns list of trades closed.
    """
    closed = []
    try:
        from paper_trader import load_trades, save_trades, mark_trade, _should_close
        import yfinance as yf
        from datetime import datetime
        import pytz
        ET  = pytz.timezone("America/New_York")
        now = datetime.now(ET)

        # Only run during market hours (9:30 AM – 4:00 PM ET)
        if not (9 * 60 + 30 <= now.hour * 60 + now.minute <= 16 * 60):
            return []

        trades      = load_trades()
        open_trades = [t for t in trades if t.get("status") == "open"]
        if not open_trades:
            return []

        # Batch price fetch
        tickers = list({t["ticker"] for t in open_trades})
        prices  = {}
        try:
            for tk in tickers:
                prices[tk] = float(yf.Ticker(tk).fast_info["lastPrice"])
        except Exception:
            pass

        now_str = datetime.now(ET).strftime("%Y-%m-%dT%H:%M")
        changed = False

        for i, trade in enumerate(trades):
            if trade["status"] != "open":
                continue
            price = prices.get(trade["ticker"])
            if not price:
                continue

            trade = mark_trade(trade, price)
            reason = _should_close(trade)

            if reason in ("stop_loss", "profit_target"):
                trade["status"]       = "closed"
                trade["close_reason"] = reason
                trade["date_closed"]  = now_str
                trade["close_source"] = "intraday_monitor"
                trades[i] = trade
                closed.append(trade)
                changed   = True
                print(f"[IntradayMonitor] {trade['ticker']} {reason} "
                      f"P&L ${trade.get('pnl', 0):+.2f} — closed intraday")

                # Close on Alpaca too if it's a stock
                if trade.get("trade_type") in ("stock_long", "stock_short"):
                    close_position(trade, reason=reason)

            else:
                trades[i] = trade

        if changed:
            save_trades(trades)

    except Exception as e:
        print(f"[IntradayMonitor] Error: {e}")

    return closed


# ── Status report ──────────────────────────────────────────────────────────────

def broker_status_embed() -> dict:
    """
    Returns a Discord embed dict showing broker connection status.
    Called by !agents or !portfolio commands.
    """
    if not broker_available():
        return {
            "title": "Broker Status",
            "description": (
                "No Alpaca keys configured.\n"
                "Add `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` to Railway env vars.\n"
                "Get keys at **alpaca.markets** → Account → API Keys (free).\n\n"
                "Stock trades are being **simulated** until keys are added.\n"
                "Options are always simulated (Alpaca stocks only)."
            ),
            "color": 0xfb923c,
        }

    acct = get_account()
    if "error" in acct:
        return {
            "title": "Broker Status — Connection Error",
            "description": f"Alpaca error: `{acct['error']}`",
            "color": 0xf87171,
        }

    mode_color = 0xfbbf24 if ALPACA_PAPER else 0xf87171  # gold=paper, red=live
    return {
        "title": f"Broker Status — Alpaca {'PAPER' if ALPACA_PAPER else 'LIVE'}",
        "description": (
            f"**Mode:** {'PAPER TRADING (safe)' if ALPACA_PAPER else '🔴 LIVE TRADING'}\n"
            f"**Status:** {acct['status']}\n"
            f"**Buying Power:** ${acct['buying_power']:,.2f}\n"
            f"**Portfolio Value:** ${acct['portfolio_value']:,.2f}\n"
            f"**Cash:** ${acct['cash']:,.2f}\n\n"
            f"*Stock trades → Alpaca | Options → Black-Scholes simulation*"
        ),
        "color": mode_color,
    }
