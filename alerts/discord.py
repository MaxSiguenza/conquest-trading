# -*- coding: utf-8 -*-
"""
Discord Alert Sender
====================
Sends richly formatted signal alerts to a Discord channel via webhook.
"""
import requests
from datetime import datetime, timezone

# Conquest Brain — optional intelligent commentary (gracefully skipped if unavailable)
def _conquest_note(r: dict) -> str:
    """Get Claude-generated analyst note for a signal. Returns empty string on failure."""
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from conquest_brain import analyze_signal
        return analyze_signal(r)
    except Exception:
        return ""


# Discord embed colors (decimal)
COLOR_GREEN  = 0x4ade80   # entry signal
COLOR_ORANGE = 0xfb923c   # MACD cross only
COLOR_GRAY   = 0x475569   # no signals
COLOR_PURPLE = 0x7c6af7   # info / test


def _result_field(r: dict) -> dict:
    """Build a Discord embed field for one ticker result."""
    ticker    = r["ticker"]
    price     = r["price"]
    chg       = r.get("today_chg_pct", 0.0)
    stale     = r.get("signal_stale", False)
    mtf       = r["mtf_score"]
    monthly   = r["monthly"]
    weekly    = r["weekly"]
    daily     = r["daily"]
    rsi       = r["rsi"]
    adx       = r["adx"]
    hvr       = r["hv_rank"]

    chg_str  = f"{'📈' if chg >= 0 else '📉'} {chg:+.1f}% today"
    hvr_note = (
        "HVR >50 → sell premium"  if hvr > 50 else
        "HVR <30 → buy options"   if hvr < 30 else
        "HVR moderate"
    )

    if stale:
        label = f"⚠️  SIGNAL STALE (down {chg:.1f}% today) — {ticker}"
        body  = (
            f"~~Entry signal~~ invalidated by today's drop\n"
            f"**${price:.2f}**  {chg_str}  |  MTF {mtf}/3  ({monthly}/{weekly}/{daily})\n"
            f"Wait for stabilization before entering."
        )
        note = _conquest_note(r)
        if note:
            body += f"\n\n⚔️ *{note}*"
    elif r.get("entry_signal"):
        mtf_stars = "★" * mtf + "☆" * (3 - mtf)
        label = f"🟢  ENTRY SIGNAL — {ticker}"
        body  = (
            f"**${price:.2f}**  {chg_str}\n"
            f"MTF {mtf}/3 {mtf_stars}  ({monthly}/{weekly}/{daily})\n"
            f"RSI {rsi:.0f}  •  ADX {adx:.0f}  •  {hvr_note}"
        )
        note = _conquest_note(r)
        if note:
            body += f"\n\n⚔️ *{note}*"
    elif r.get("macd_cross_up"):
        label = f"🟡  MACD Cross ↑ — {ticker}"
        body  = (
            f"**${price:.2f}**  {chg_str}\n"
            f"MTF {mtf}/3  ({monthly}/{weekly}/{daily})\n"
            f"RSI {rsi:.0f}  •  ADX {adx:.0f}  •  {hvr_note}"
        )
        note = _conquest_note(r)
        if note:
            body += f"\n\n⚔️ *{note}*"
    else:
        label = f"⚪  {ticker}"
        body  = f"${price:.2f}  {chg_str}  |  MTF {mtf}/3  |  RSI {rsi:.0f}  |  No signal"

    return {"name": label, "value": body, "inline": False}


def send_discord_alert(
    webhook_url: str,
    results: list,
    only_signals: bool = True,
) -> tuple[bool, str]:
    """
    Send a Discord embed with scan results.

    Parameters
    ----------
    webhook_url  : Discord channel webhook URL
    results      : list of dicts from scanner.scan_watchlist()
    only_signals : if True, only include tickers with active signals

    Returns (success: bool, message: str)
    """
    if not webhook_url or not webhook_url.startswith("https://discord.com/api/webhooks/"):
        return False, "Invalid webhook URL. Must start with https://discord.com/api/webhooks/"

    entries = [r for r in results if not r.get("error") and r.get("entry_signal")]
    crosses = [r for r in results if not r.get("error") and not r.get("entry_signal") and r.get("macd_cross_up")]
    errors  = [r for r in results if r.get("error")]

    if only_signals:
        to_show = entries + crosses
    else:
        to_show = [r for r in results if not r.get("error")]

    fields = [_result_field(r) for r in to_show] if to_show else []

    if not fields:
        fields = [{
            "name": "No Active Signals Today",
            "value": (
                f"Scanned **{len(results)}** tickers — "
                "no entry signals or MACD crossovers detected.\n"
                "Stay patient. Wait for the setup."
            ),
            "inline": False,
        }]

    if errors:
        fields.append({
            "name": f"⚠️  {len(errors)} ticker(s) failed",
            "value": ", ".join(r["ticker"] for r in errors),
            "inline": False,
        })

    # Pick embed color based on what fired
    color = COLOR_GREEN if entries else COLOR_ORANGE if crosses else COLOR_GRAY

    now_utc = datetime.now(timezone.utc)
    scanned = len([r for r in results if not r.get("error")])
    desc = (
        f"Scanned **{scanned}** tickers  •  "
        f"**{len(entries)}** entry signal(s)  •  "
        f"**{len(crosses)}** MACD cross(es)"
    )

    embed = {
        "title": "⚔️  Conquest Signal Scan",
        "description": desc,
        "color": color,
        "fields": fields,
        "footer": {
            "text": "Conquest Trading  •  Not financial advice  •  Always do your own research"
        },
        "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    try:
        resp = requests.post(
            webhook_url,
            json={"embeds": [embed]},
            timeout=10,
        )
        if resp.status_code == 204:
            return True, "Alert sent successfully!"
        else:
            return False, f"Discord returned HTTP {resp.status_code}: {resp.text[:200]}"
    except requests.RequestException as e:
        return False, f"Network error: {e}"


def test_webhook(webhook_url: str) -> tuple[bool, str]:
    """Send a simple test ping to verify the webhook URL works."""
    if not webhook_url or not webhook_url.startswith("https://discord.com/api/webhooks/"):
        return False, "Invalid webhook URL."
    try:
        resp = requests.post(
            webhook_url,
            json={"content": "✅  Quant Dashboard connected! You'll receive signal alerts here."},
            timeout=10,
        )
        if resp.status_code == 204:
            return True, "Test message sent! Check your Discord channel."
        return False, f"Discord returned HTTP {resp.status_code}"
    except requests.RequestException as e:
        return False, f"Network error: {e}"
