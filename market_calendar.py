# -*- coding: utf-8 -*-
"""
market_calendar.py — Conquest Trading
======================================
Single source of truth for:
  - NYSE market holidays (is_trading_day)
  - Upcoming economic events (FOMC, CPI, NFP, etc.)
  - Human-readable "today's market context" for morning briefs

Usage:
    from market_calendar import is_trading_day, today_context, upcoming_events
"""

from __future__ import annotations
from datetime import date, timedelta
from typing import Optional


# ── NYSE Holidays ─────────────────────────────────────────────────────────────
# Full-day closures only. Half-days (e.g. day before Christmas) are NOT included
# because markets are still open and trades can execute.
# Update annually each December for the following year.

_NYSE_HOLIDAYS: frozenset = frozenset({
    # 2025
    date(2025, 1,  1),   # New Year's Day
    date(2025, 1, 20),   # MLK Day
    date(2025, 2, 17),   # Presidents' Day
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 26),   # Memorial Day
    date(2025, 6, 19),   # Juneteenth
    date(2025, 7,  4),   # Independence Day
    date(2025, 9,  1),   # Labor Day
    date(2025, 11, 27),  # Thanksgiving
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1,  1),   # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 4,  3),   # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7,  3),   # Independence Day (observed — July 4 falls on Saturday)
    date(2026, 9,  7),   # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
})

# Holiday display names
_HOLIDAY_NAMES: dict[date, str] = {
    date(2025, 1,  1): "New Year's Day",
    date(2025, 1, 20): "MLK Day",
    date(2025, 2, 17): "Presidents' Day",
    date(2025, 4, 18): "Good Friday",
    date(2025, 5, 26): "Memorial Day",
    date(2025, 6, 19): "Juneteenth",
    date(2025, 7,  4): "Independence Day",
    date(2025, 9,  1): "Labor Day",
    date(2025, 11, 27): "Thanksgiving",
    date(2025, 12, 25): "Christmas",
    date(2026, 1,  1): "New Year's Day",
    date(2026, 1, 19): "MLK Day",
    date(2026, 2, 16): "Presidents' Day",
    date(2026, 4,  3): "Good Friday",
    date(2026, 5, 25): "Memorial Day",
    date(2026, 6, 19): "Juneteenth",
    date(2026, 7,  3): "Independence Day (observed)",
    date(2026, 9,  7): "Labor Day",
    date(2026, 11, 26): "Thanksgiving",
    date(2026, 12, 25): "Christmas",
}


# ── Economic Calendar ─────────────────────────────────────────────────────────
# Key macro events that affect market volatility and trade decisions.
# Format: (date, event_name, importance, note)
# importance: "HIGH" | "MEDIUM" | "LOW"
# Update annually. FOMC dates are set in advance by the Fed.

_ECONOMIC_EVENTS: list[tuple] = [
    # ── FOMC Meeting Dates 2026 (rate decision days — last day of each meeting) ──
    (date(2026, 1, 29), "FOMC Rate Decision", "HIGH", "Federal Reserve interest rate decision"),
    (date(2026, 3, 19), "FOMC Rate Decision", "HIGH", "Federal Reserve interest rate decision"),
    (date(2026, 5,  7), "FOMC Rate Decision", "HIGH", "Federal Reserve interest rate decision"),
    (date(2026, 6, 18), "FOMC Rate Decision", "HIGH", "Federal Reserve interest rate decision"),
    (date(2026, 7, 30), "FOMC Rate Decision", "HIGH", "Federal Reserve interest rate decision"),
    (date(2026, 9, 17), "FOMC Rate Decision", "HIGH", "Federal Reserve interest rate decision"),
    (date(2026, 11,  5), "FOMC Rate Decision", "HIGH", "Federal Reserve interest rate decision"),
    (date(2026, 12, 17), "FOMC Rate Decision", "HIGH", "Federal Reserve interest rate decision"),

    # ── CPI (Consumer Price Index) — approx 2nd week of each month ──
    (date(2026, 1, 14), "CPI Report", "HIGH", "Consumer Price Index — key inflation gauge"),
    (date(2026, 2, 11), "CPI Report", "HIGH", "Consumer Price Index — key inflation gauge"),
    (date(2026, 3, 11), "CPI Report", "HIGH", "Consumer Price Index — key inflation gauge"),
    (date(2026, 4,  9), "CPI Report", "HIGH", "Consumer Price Index — key inflation gauge"),
    (date(2026, 5, 13), "CPI Report", "HIGH", "Consumer Price Index — key inflation gauge"),
    (date(2026, 6, 10), "CPI Report", "HIGH", "Consumer Price Index — key inflation gauge"),
    (date(2026, 7,  9), "CPI Report", "HIGH", "Consumer Price Index — key inflation gauge"),
    (date(2026, 8, 12), "CPI Report", "HIGH", "Consumer Price Index — key inflation gauge"),
    (date(2026, 9,  9), "CPI Report", "HIGH", "Consumer Price Index — key inflation gauge"),
    (date(2026, 10, 14), "CPI Report", "HIGH", "Consumer Price Index — key inflation gauge"),
    (date(2026, 11, 12), "CPI Report", "HIGH", "Consumer Price Index — key inflation gauge"),
    (date(2026, 12,  9), "CPI Report", "HIGH", "Consumer Price Index — key inflation gauge"),

    # ── NFP / Jobs Report — first Friday of each month ──
    (date(2026, 1,  2), "NFP Jobs Report", "HIGH", "Non-Farm Payrolls — labor market health"),
    (date(2026, 2,  6), "NFP Jobs Report", "HIGH", "Non-Farm Payrolls — labor market health"),
    (date(2026, 3,  6), "NFP Jobs Report", "HIGH", "Non-Farm Payrolls — labor market health"),
    (date(2026, 4,  3), "NFP Jobs Report", "HIGH", "Non-Farm Payrolls — labor market health"),
    (date(2026, 5,  1), "NFP Jobs Report", "HIGH", "Non-Farm Payrolls — labor market health"),
    (date(2026, 6,  5), "NFP Jobs Report", "HIGH", "Non-Farm Payrolls — labor market health"),
    (date(2026, 7, 10), "NFP Jobs Report", "HIGH", "Non-Farm Payrolls — labor market health"),
    (date(2026, 8,  7), "NFP Jobs Report", "HIGH", "Non-Farm Payrolls — labor market health"),
    (date(2026, 9,  4), "NFP Jobs Report", "HIGH", "Non-Farm Payrolls — labor market health"),
    (date(2026, 10,  2), "NFP Jobs Report", "HIGH", "Non-Farm Payrolls — labor market health"),
    (date(2026, 11,  6), "NFP Jobs Report", "HIGH", "Non-Farm Payrolls — labor market health"),
    (date(2026, 12,  4), "NFP Jobs Report", "HIGH", "Non-Farm Payrolls — labor market health"),

    # ── PCE (Personal Consumption Expenditures) — Fed's preferred inflation gauge ──
    (date(2026, 1, 30), "PCE Inflation", "HIGH", "Fed's preferred inflation measure"),
    (date(2026, 2, 27), "PCE Inflation", "HIGH", "Fed's preferred inflation measure"),
    (date(2026, 3, 27), "PCE Inflation", "HIGH", "Fed's preferred inflation measure"),
    (date(2026, 4, 30), "PCE Inflation", "HIGH", "Fed's preferred inflation measure"),
    (date(2026, 5, 29), "PCE Inflation", "HIGH", "Fed's preferred inflation measure"),
    (date(2026, 6, 26), "PCE Inflation", "HIGH", "Fed's preferred inflation measure"),
    (date(2026, 7, 31), "PCE Inflation", "HIGH", "Fed's preferred inflation measure"),
    (date(2026, 8, 28), "PCE Inflation", "HIGH", "Fed's preferred inflation measure"),
    (date(2026, 9, 25), "PCE Inflation", "HIGH", "Fed's preferred inflation measure"),
    (date(2026, 10, 30), "PCE Inflation", "HIGH", "Fed's preferred inflation measure"),
    (date(2026, 11, 25), "PCE Inflation", "HIGH", "Fed's preferred inflation measure"),
    (date(2026, 12, 23), "PCE Inflation", "HIGH", "Fed's preferred inflation measure"),

    # ── GDP Reports — quarterly ──
    (date(2026, 1, 29), "GDP Advance Q4 2025", "MEDIUM", "Advance estimate of Q4 2025 GDP growth"),
    (date(2026, 4, 29), "GDP Advance Q1 2026", "MEDIUM", "Advance estimate of Q1 2026 GDP growth"),
    (date(2026, 7, 30), "GDP Advance Q2 2026", "MEDIUM", "Advance estimate of Q2 2026 GDP growth"),
    (date(2026, 10, 29), "GDP Advance Q3 2026", "MEDIUM", "Advance estimate of Q3 2026 GDP growth"),

    # ── Earnings Seasons (approximate windows) — not individual companies ──
    # These flag the weeks where ~80% of S&P 500 reports
    (date(2026, 1, 12), "Earnings Season Start (Q4 2025)", "MEDIUM", "Major banks kick off earnings season"),
    (date(2026, 4, 13), "Earnings Season Start (Q1 2026)", "MEDIUM", "Major banks kick off earnings season"),
    (date(2026, 7, 13), "Earnings Season Start (Q2 2026)", "MEDIUM", "Major banks kick off earnings season"),
    (date(2026, 10, 12), "Earnings Season Start (Q3 2026)", "MEDIUM", "Major banks kick off earnings season"),
]


# ── Core API ───────────────────────────────────────────────────────────────────

def is_trading_day(dt: Optional[date] = None) -> bool:
    """Return True if dt is a NYSE trading day (weekday, not a holiday)."""
    if dt is None:
        dt = date.today()
    if hasattr(dt, "date"):   # datetime → date
        dt = dt.date()
    return dt.weekday() < 5 and dt not in _NYSE_HOLIDAYS


def holiday_name(dt: Optional[date] = None) -> Optional[str]:
    """Return the holiday name for dt, or None if it's a trading day."""
    if dt is None:
        dt = date.today()
    if hasattr(dt, "date"):
        dt = dt.date()
    return _HOLIDAY_NAMES.get(dt)


def next_trading_day(dt: Optional[date] = None) -> date:
    """Return the next trading day after dt (or today if today is a trading day)."""
    if dt is None:
        dt = date.today()
    if hasattr(dt, "date"):
        dt = dt.date()
    candidate = dt + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def prev_trading_day(dt: Optional[date] = None) -> date:
    """Return the most recent trading day before dt."""
    if dt is None:
        dt = date.today()
    if hasattr(dt, "date"):
        dt = dt.date()
    candidate = dt - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def upcoming_events(days: int = 7, from_date: Optional[date] = None) -> list[dict]:
    """
    Return economic events in the next `days` calendar days.
    Each entry: {date, name, importance, note, days_away}
    Sorted by date ascending.
    """
    if from_date is None:
        from_date = date.today()
    if hasattr(from_date, "date"):
        from_date = from_date.date()
    end = from_date + timedelta(days=days)
    result = []
    for ev_date, name, importance, note in _ECONOMIC_EVENTS:
        if from_date <= ev_date <= end:
            result.append({
                "date":       ev_date.isoformat(),
                "name":       name,
                "importance": importance,
                "note":       note,
                "days_away":  (ev_date - from_date).days,
            })
    return sorted(result, key=lambda x: x["date"])


def today_context() -> dict:
    """
    Return a dict describing today's market status and upcoming events.
    Used by morning_brief and conquest_brain for Claude context.

    Returns:
        {
            "is_trading_day": bool,
            "holiday_name": str | None,
            "next_trading_day": str,       # ISO date
            "upcoming_events": [...],      # next 7 days
            "today_events": [...],         # events today
            "summary": str,                # human-readable one-liner
        }
    """
    today = date.today()
    trading = is_trading_day(today)
    hname   = holiday_name(today)
    next_td = next_trading_day(today)
    events  = upcoming_events(days=7)
    today_evs = [e for e in events if e["date"] == today.isoformat()]
    upcoming  = [e for e in events if e["date"] != today.isoformat()]

    if not trading and hname:
        summary = f"Markets closed today — {hname}. Next trading day: {next_td.strftime('%A, %b %d')}."
    elif not trading:
        summary = f"Markets closed today (weekend). Next trading day: {next_td.strftime('%A, %b %d')}."
    elif today_evs:
        event_names = ", ".join(e["name"] for e in today_evs)
        summary = f"Trading day — HIGH IMPACT events today: {event_names}. Expect elevated volatility."
    else:
        if upcoming:
            next_ev = upcoming[0]
            summary = (f"Normal trading day. "
                       f"Next key event: {next_ev['name']} in {next_ev['days_away']} day(s).")
        else:
            summary = "Normal trading day. No major economic events in the next 7 days."

    return {
        "is_trading_day":  trading,
        "holiday_name":    hname,
        "next_trading_day": next_td.isoformat(),
        "upcoming_events": upcoming,
        "today_events":    today_evs,
        "summary":         summary,
    }
