# -*- coding: utf-8 -*-
"""
Conquest Trading — Notion Trade Journal
=========================================
Auto-logs every paper trade open and close to the Notion Trade Journal database.

Required Railway env vars:
  NOTION_TOKEN        — Notion internal integration token (from notion.so/my-integrations)
  NOTION_TRADE_DB_ID  — Trade Journal database ID (default pre-filled below)

Usage:
  from notion_journal import log_trade_open, log_trade_close
"""

import os

# Pre-filled with the created database ID — override via env var if you move it
NOTION_TRADE_DB_ID = os.getenv(
    "NOTION_TRADE_DB_ID", "1fd3c4a03ae04364b4c85e756ccf10c6"
)

_client = None


def _get_client():
    """Lazy-init the Notion client. Returns None if token not configured."""
    global _client
    if _client is not None:
        return _client
    try:
        from notion_client import Client
    except ImportError:
        print("[Notion] notion-client not installed. Add it to requirements.txt.")
        return None

    token = os.getenv("NOTION_TOKEN", "")
    if not token:
        print("[Notion] NOTION_TOKEN not set — skipping Notion logging.")
        return None

    _client = Client(auth=token)
    return _client


# ── Open a new trade ──────────────────────────────────────────────────────────

def log_trade_open(trade: dict) -> bool:
    """
    Create a new row in the Trade Journal for a freshly opened paper trade.
    Safe to call even if Notion isn't configured — silently returns False.
    """
    client = _get_client()
    if not client:
        return False

    try:
        date_opened = (trade.get("date_entered") or "")[:10]

        properties = {
            "Trade ID":  {"title": [{"text": {"content": trade["id"]}}]},
            "Ticker":    {"rich_text": [{"text": {"content": trade["ticker"]}}]},
            "Trade Type": {"select": {"name": trade["trade_type"]}},
            "Status":    {"select": {"name": "open"}},
            "Cost Basis": {"number": float(trade.get("cost_basis") or 0)},
            "MTF Score": {"number": float(trade.get("mtf_score") or 0)},
            "RSI Entry": {"number": float(trade.get("rsi_entry") or 0)},
            "ADX Entry": {"number": float(trade.get("adx_entry") or 0)},
            "Entry Reasoning": {
                "rich_text": [{"text": {"content": (trade.get("reasoning") or "")[:2000]}}]
            },
        }
        if date_opened:
            properties["Date Opened"] = {"date": {"start": date_opened}}

        client.pages.create(
            parent={"database_id": NOTION_TRADE_DB_ID},
            properties=properties,
        )
        return True

    except Exception as e:
        print(f"[Notion] log_trade_open failed for {trade.get('id')}: {e}")
        return False


# ── Close an existing trade ───────────────────────────────────────────────────

def log_trade_close(trade: dict) -> bool:
    """
    Update the Notion row when a trade closes.
    If the row doesn't exist yet (app deployed before Notion was added),
    creates a new closed row instead.
    """
    client = _get_client()
    if not client:
        return False

    try:
        date_closed = (trade.get("date_closed") or "")[:10]

        update_props = {
            "Status":      {"select": {"name": "closed"}},
            "PnL":         {"number": float(trade.get("pnl") or 0)},
            "PnL Pct":     {"number": round(float(trade.get("pnl_pct") or 0) * 100, 2)},
            "Days Held":   {"number": int(trade.get("days_held") or 0)},
            "Close Reason": {"select": {"name": trade.get("close_reason") or "manual"}},
            "Close Reasoning": {
                "rich_text": [{"text": {"content": (trade.get("close_reasoning") or "")[:2000]}}]
            },
        }
        if date_closed:
            update_props["Date Closed"] = {"date": {"start": date_closed}}

        # Find existing row by Trade ID
        results = client.databases.query(
            database_id=NOTION_TRADE_DB_ID,
            filter={"property": "Trade ID", "title": {"equals": trade["id"]}},
        )

        if results.get("results"):
            page_id = results["results"][0]["id"]
            client.pages.update(page_id=page_id, properties=update_props)
        else:
            # Row not found — create a complete closed row
            update_props.update({
                "Trade ID":   {"title": [{"text": {"content": trade["id"]}}]},
                "Ticker":     {"rich_text": [{"text": {"content": trade["ticker"]}}]},
                "Trade Type": {"select": {"name": trade["trade_type"]}},
                "Cost Basis": {"number": float(trade.get("cost_basis") or 0)},
            })
            if (trade.get("date_entered") or "")[:10]:
                update_props["Date Opened"] = {"date": {"start": trade["date_entered"][:10]}}
            client.pages.create(
                parent={"database_id": NOTION_TRADE_DB_ID},
                properties=update_props,
            )

        return True

    except Exception as e:
        print(f"[Notion] log_trade_close failed for {trade.get('id')}: {e}")
        return False
