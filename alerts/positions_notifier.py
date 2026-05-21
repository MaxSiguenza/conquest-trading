# -*- coding: utf-8 -*-
"""
Conquest Trading — Position Discord Notifier
=============================================
Sends position add/close/profit alerts to a dedicated Discord channel.
Called automatically when positions are created or updated via the web UI.
"""
import requests
import json
import os
from datetime import datetime, timezone

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "alerts_settings.json")

COLOR_GREEN  = 0x4ade80
COLOR_RED    = 0xf87171
COLOR_PURPLE = 0x7c6af7
COLOR_ORANGE = 0xfb923c
COLOR_GOLD   = 0xfbbf24


def _get_positions_webhook() -> str:
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
        return s.get("positions_webhook", "").strip()
    except Exception:
        return ""


def _post(webhook_url: str, payload: dict) -> bool:
    if not webhook_url or not webhook_url.startswith("https://discord.com/api/webhooks/"):
        return False
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        return resp.status_code == 204
    except Exception:
        return False


def notify_position_opened(p: dict) -> bool:
    """
    Send a Discord embed when a new position is opened.
    p = the position dict as stored in positions.json
    """
    webhook = _get_positions_webhook()
    if not webhook:
        return False

    kind = p.get("kind", "unknown")
    ticker = p.get("ticker", "?")

    if kind == "spread":
        ls       = p["long_strike"]
        ss       = p["short_strike"]
        opt_type = p["option_type"].upper()
        net_cost = p["net_cost"]
        expiry   = p["expiry"]
        contracts = p.get("contracts", 1)
        cost_total = abs(net_cost) * contracts * 100
        is_debit = net_cost > 0

        if is_debit:
            type_label = f"Bull {opt_type} Spread (Debit)"
            direction  = f"Paid **${cost_total:.0f}** upfront"
        else:
            type_label = f"Bull {opt_type} Spread (Credit)"
            direction  = f"Collected **${cost_total:.0f}** credit"

        width      = abs(ss - ls)
        max_profit = (width - abs(net_cost)) * contracts * 100 if is_debit else cost_total
        max_loss   = cost_total if is_debit else (width - abs(net_cost)) * contracts * 100
        breakeven  = ls + abs(net_cost) if is_debit else ss - abs(net_cost)

        description = (
            f"**{ticker}** — {type_label}\n"
            f"Legs: ${ls} / ${ss} {opt_type}  •  Expires {expiry}  •  {contracts} contract\n"
            f"{direction}  •  Max profit **${max_profit:.0f}**  •  Max loss **${max_loss:.0f}**\n"
            f"Break-even: **${breakeven:.2f}**"
        )
        color = COLOR_GREEN if is_debit else COLOR_PURPLE

    elif kind == "option":
        strike    = p["strike"]
        opt_type  = p["option_type"].upper()
        expiry    = p["expiry"]
        contracts = p.get("contracts", 1)
        premium   = p["premium"]
        cost_total = premium * contracts * 100

        description = (
            f"**{ticker}** — {opt_type} Option\n"
            f"Strike: ${strike}  •  Expires {expiry}  •  {contracts} contract\n"
            f"Paid **${cost_total:.0f}** (${premium:.2f}/share)"
        )
        color = COLOR_GREEN

    elif kind == "stock":
        entry  = p["entry_price"]
        shares = p["shares"]
        cost   = entry * shares
        description = (
            f"**{ticker}** — Stock Position\n"
            f"{shares:.0f} shares @ **${entry:.2f}**\n"
            f"Cost basis: **${cost:.0f}**"
        )
        color = COLOR_GREEN
    else:
        return False

    embed = {
        "title": "📥  New Position Opened",
        "description": description,
        "color": color,
        "footer": {"text": "Conquest Trading  •  Paper Portfolio"},
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return _post(webhook, {"embeds": [embed]})


def notify_position_closed(p: dict, pnl: float, pnl_pct: float, reason: str = "Manually closed") -> bool:
    """
    Send a Discord embed when a position is removed/closed.
    """
    webhook = _get_positions_webhook()
    if not webhook:
        return False

    ticker = p.get("ticker", "?")
    kind   = p.get("kind", "unknown")
    color  = COLOR_GREEN if pnl >= 0 else COLOR_RED
    sign   = "+" if pnl >= 0 else ""
    result = "✅  PROFIT" if pnl >= 0 else "❌  LOSS"

    if kind == "spread":
        trade_desc = f"${p['long_strike']}/{p['short_strike']} {p['option_type'].upper()} Spread"
    elif kind == "option":
        trade_desc = f"${p['strike']} {p['option_type'].upper()}"
    else:
        trade_desc = "Stock"

    description = (
        f"**{ticker}** — {trade_desc}\n"
        f"Result: **{sign}${pnl:.0f}** ({sign}{pnl_pct*100:.1f}%)  •  {result}\n"
        f"Reason: {reason}"
    )

    embed = {
        "title": "📤  Position Closed",
        "description": description,
        "color": color,
        "footer": {"text": "Conquest Trading  •  Paper Portfolio"},
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return _post(webhook, {"embeds": [embed]})


def notify_profit_target(p: dict, pnl: float, pnl_pct: float) -> bool:
    """
    Send an urgent alert when a position hits 50% profit target.
    """
    webhook = _get_positions_webhook()
    if not webhook:
        return False

    ticker = p.get("ticker", "?")
    if p.get("kind") == "spread":
        trade_desc = f"${p['long_strike']}/{p['short_strike']} {p['option_type'].upper()} Spread"
        close_note = f"Close the spread — buy back at ${p.get('cur_spread', 0)*100:.2f} limit"
    elif p.get("kind") == "option":
        trade_desc = f"${p['strike']} {p['option_type'].upper()}"
        close_note = "Sell to close — don't let winners turn into losers"
    else:
        trade_desc = "position"
        close_note = "Consider trimming or taking full profit"

    description = (
        f"**{ticker}** — {trade_desc}\n"
        f"P&L: **+${pnl:.0f}** (+{pnl_pct*100:.1f}%) — **50% TARGET HIT**\n"
        f"⚡ {close_note}"
    )

    embed = {
        "title": "🎯  TAKE PROFIT — 50% Target Hit",
        "description": description,
        "color": COLOR_GOLD,
        "footer": {"text": "Conquest Trading  •  Close now, redeploy capital"},
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return _post(webhook, {"embeds": [embed]})


def notify_daily_pnl(portfolio: dict) -> bool:
    """
    Send end-of-day P&L summary to Discord.
    Called manually from the portfolio page or via scheduler.
    """
    webhook = _get_positions_webhook()
    if not webhook:
        return False

    total_cost  = portfolio.get("total_cost",  0)
    total_value = portfolio.get("total_value", 0)
    total_pnl   = portfolio.get("total_pnl",   0)
    total_pct   = portfolio.get("total_pnl_pct", 0) * 100
    vix         = portfolio.get("vix", 0)
    positions   = portfolio.get("positions", [])
    sign        = "+" if total_pnl >= 0 else ""
    color       = COLOR_GREEN if total_pnl >= 0 else COLOR_RED

    # Build per-position lines
    lines = []
    for p in positions:
        if p.get("error") or not p.get("kind"):
            continue
        pnl   = p.get("pnl", 0)
        psign = "+" if pnl >= 0 else ""
        if p["kind"] == "spread":
            lines.append(
                f"**{p['ticker']}** {p.get('type_label','Spread')} — "
                f"{psign}${pnl:.0f} ({psign}{p.get('pnl_pct',0)*100:.1f}%)  •  "
                f"{p.get('dte','?')}d left  •  {p.get('recommendation','')}"
            )
        elif p["kind"] == "option":
            lines.append(
                f"**{p['ticker']}** {p['option_type'].upper()} ${p['strike']} — "
                f"{psign}${pnl:.0f} ({psign}{p.get('pnl_pct',0)*100:.1f}%)  •  "
                f"{p.get('dte','?')}d left"
            )
        elif p["kind"] == "stock":
            lines.append(
                f"**{p['ticker']}** stock — "
                f"{psign}${pnl:.0f} ({psign}{p.get('pnl_pct',0)*100:.1f}%)"
            )

    pos_block = "\n".join(lines) if lines else "No open positions."

    fields = [
        {"name": "Open Positions", "value": pos_block, "inline": False},
        {"name": "Cost Basis",     "value": f"${total_cost:,.0f}",  "inline": True},
        {"name": "Current Value",  "value": f"${total_value:,.0f}", "inline": True},
        {"name": "VIX",            "value": str(vix),               "inline": True},
    ]

    embed = {
        "title": f"📊  Daily P&L — {sign}${total_pnl:,.0f} ({sign}{total_pct:.1f}%)",
        "color": color,
        "fields": fields,
        "footer": {"text": "Conquest Trading  •  Paper Portfolio"},
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return _post(webhook, {"embeds": [embed]})
