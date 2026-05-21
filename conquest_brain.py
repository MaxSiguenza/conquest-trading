# -*- coding: utf-8 -*-
"""
Conquest Brain — Claude API Intelligence Layer
==============================================
Turns raw signal data into analyst-grade commentary.
Used by Discord alerts, morning briefings, and the Q&A bot.
"""
import os
from dotenv import load_dotenv
import anthropic

_ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(_ENV_FILE, override=True)

_client = None

def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # Try dotenv_values first (always reads from file, not cached env)
        from dotenv import dotenv_values
        vals = dotenv_values(_ENV_FILE)
        key  = vals.get("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")
        if not key or key == "paste-your-new-key-here":
            raise RuntimeError("ANTHROPIC_API_KEY not set in .env file.")
        _client = anthropic.Anthropic(api_key=key)
    return _client


if __name__ == "__main__":
    print("Testing Conquest Brain API connection...")
    client = _get_client()
    msg = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=30,
        messages=[{"role": "user", "content": "Say exactly: Conquest Trading online. All systems go."}]
    )
    print(msg.content[0].text)
    print("\nAPI connection confirmed. Conquest Brain is ready.")


SYSTEM_PROMPT = """You are the Conquest intelligence engine — a sharp, concise quantitative trading analyst.
You have deep knowledge of technical analysis, options strategy, and macro conditions.
You model your thinking after Michael Burry (contrarian conviction from data),
Warren Buffett (understanding true value vs. market price), and systematic traders
who let the data speak without ego.

Rules:
- Be direct and specific. No fluff, no generic statements.
- Reference the actual numbers in your response.
- Never say "this is not financial advice" — the user already knows.
- Keep responses tight. Say more with less.
- When signals are weak, say so plainly. Don't hype mediocre setups.
- When signals are strong, show conviction. Don't hedge excessively.
- You have access to the user's actual position and watchlist data."""


def analyze_signal(result: dict) -> str:
    """
    Generate a 2-3 sentence analyst note for a single scan result.
    Used to enrich Discord alert embeds with intelligence.
    """
    ticker    = result.get("ticker", "?")
    price     = result.get("price", 0)
    chg       = result.get("today_chg_pct", 0)
    mtf       = result.get("mtf_score", 0)
    monthly   = result.get("monthly", "?")
    weekly    = result.get("weekly", "?")
    daily     = result.get("daily", "?")
    rsi       = result.get("rsi", 50)
    adx       = result.get("adx", 0)
    hvr       = result.get("hv_rank", 50)
    entry     = result.get("entry_signal", False)
    macd_x    = result.get("macd_cross_up", False)
    stale     = result.get("signal_stale", False)

    signal_type = (
        "STALE ENTRY SIGNAL (stock moving against signal today)" if stale else
        "ENTRY SIGNAL (all conditions met)" if entry else
        "MACD crossover (momentum shifting, entry conditions not fully met)" if macd_x else
        "no active signal"
    )

    prompt = f"""Ticker: {ticker}
Price: ${price:.2f}  |  Today: {chg:+.1f}%
Signal: {signal_type}
MTF Score: {mtf}/3  ({monthly} monthly / {weekly} weekly / {daily} daily)
RSI: {rsi:.0f}  |  ADX: {adx:.0f}  |  HV Rank: {hvr:.0f}/100

Write exactly 2-3 sentences of analyst commentary. Be specific to these numbers.
If it's an entry signal, explain what makes it compelling or what the key risk is.
If it's a MACD cross only, explain what needs to happen to confirm entry.
If the signal is stale (stock dropping against the signal), be direct about the risk.
End with one specific thing to watch."""

    try:
        msg = _get_client().messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"[Analysis unavailable: {e}]"


def morning_briefing(scan_results: list, macro_notes: str = "") -> str:
    """
    Generate a full morning briefing from a watchlist scan.
    Sent to Discord at market open. Returns formatted text.
    """
    entries = [r for r in scan_results if r.get("entry_signal") and not r.get("error")]
    crosses = [r for r in scan_results if r.get("macd_cross_up") and not r.get("entry_signal") and not r.get("error")]
    total   = len([r for r in scan_results if not r.get("error")])

    signal_lines = []
    for r in entries[:5]:
        signal_lines.append(
            f"  ENTRY: {r['ticker']} ${r['price']:.2f} ({r['today_chg_pct']:+.1f}%) "
            f"MTF {r['mtf_score']}/3 RSI {r['rsi']:.0f} HVR {r['hv_rank']:.0f}"
        )
    for r in crosses[:3]:
        signal_lines.append(
            f"  MACD X: {r['ticker']} ${r['price']:.2f} ({r['today_chg_pct']:+.1f}%) "
            f"MTF {r['mtf_score']}/3"
        )

    signals_text = "\n".join(signal_lines) if signal_lines else "  No active signals today."

    prompt = f"""Today's watchlist scan ({total} tickers):
{signals_text}

{"Macro context: " + macro_notes if macro_notes else ""}

Write a tight morning briefing (4-6 sentences). Cover:
1. What the signal picture looks like today (busy/quiet, quality of setups)
2. The 1-2 most actionable setups if any, with specific reasoning
3. What to watch or avoid today
4. One forward-looking note (what would change your view)

Write in the voice of a sharp desk analyst giving a 60-second morning rundown.
No bullet points — flowing sentences. Be direct."""

    try:
        msg = _get_client().messages.create(
            model="claude-sonnet-4-5",
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"[Morning briefing unavailable: {e}]"


def answer_question(question: str, context: dict) -> str:
    """
    Answer a user question using their real portfolio and watchlist data.
    Used by the Discord Q&A bot in #ask-wolf.

    context dict can include:
      positions    : list of position dicts from get_positions_web_data()
      scan_results : latest watchlist scan
      portfolio    : portfolio summary dict
    """
    positions   = context.get("positions", [])
    scan        = context.get("scan_results", [])
    portfolio   = context.get("portfolio", {})

    # Format positions for context
    pos_lines = []
    for p in positions:
        if p.get("error"):
            continue
        if p.get("kind") == "spread":
            pos_lines.append(
                f"  {p['ticker']} {p['type_label']} "
                f"${p['long_strike']}/{p['short_strike']} exp {p['expiry']} "
                f"({p['dte']}d) | cost ${p['cost']:.0f} | P&L {p['pnl']:+.0f} ({p['pnl_pct']*100:+.1f}%) "
                f"| BE ${p['breakeven']:.2f} | now ${p['current_price']:.2f} | {p['recommendation']}"
            )
        elif p.get("kind") == "option":
            pos_lines.append(
                f"  {p['ticker']} {p['option_type'].upper()} ${p['strike']} exp {p['expiry']} "
                f"({p['dte']}d) | cost ${p['cost']:.0f} | P&L {p['pnl']:+.0f} ({p['pnl_pct']*100:+.1f}%) "
                f"| {p['recommendation']}"
            )
        elif p.get("kind") == "stock":
            pos_lines.append(
                f"  {p['ticker']} stock {p['shares']:.0f}sh @ ${p['entry_price']:.2f} "
                f"| now ${p['current_price']:.2f} | P&L {p['pnl']:+.0f} ({p['pnl_pct']*100:+.1f}%) "
                f"| {p['recommendation']}"
            )

    scan_lines = []
    for r in scan[:8]:
        if r.get("error"):
            continue
        sig = "ENTRY" if r.get("entry_signal") else "MACD X" if r.get("macd_cross_up") else "—"
        scan_lines.append(
            f"  {r['ticker']} ${r['price']:.2f} {r['today_chg_pct']:+.1f}% "
            f"MTF {r['mtf_score']}/3 RSI {r['rsi']:.0f} [{sig}]"
        )

    total_pnl = portfolio.get("total_pnl", 0)
    total_cost = portfolio.get("total_cost", 0)

    context_block = f"""CURRENT POSITIONS:
{chr(10).join(pos_lines) if pos_lines else "  No open positions."}

LATEST WATCHLIST SCAN:
{chr(10).join(scan_lines) if scan_lines else "  No scan data available."}

PORTFOLIO: Cost basis ${total_cost:.0f} | Total P&L {total_pnl:+.0f}"""

    try:
        msg = _get_client().messages.create(
            model="claude-sonnet-4-5",
            max_tokens=350,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"{context_block}\n\nUser question: {question}"
            }],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"[Could not answer: {e}]"


def position_debrief(positions: list, portfolio: dict) -> str:
    """
    Evening debrief — summary of all positions and what to do next.
    """
    pos_lines = []
    for p in positions:
        if p.get("error") or not p.get("kind"):
            continue
        if p.get("kind") == "spread":
            pos_lines.append(
                f"  {p['ticker']} {p.get('type_label','spread')} "
                f"${p['long_strike']}/{p['short_strike']} | "
                f"P&L {p['pnl']:+.0f} ({p['pnl_pct']*100:+.1f}%) | "
                f"{p['dte']}d left | BE ${p['breakeven']:.2f} vs now ${p['current_price']:.2f} | "
                f"{p['recommendation']}"
            )
        elif p.get("kind") == "option":
            pos_lines.append(
                f"  {p['ticker']} {p['option_type'].upper()} ${p['strike']} | "
                f"P&L {p['pnl']:+.0f} ({p['pnl_pct']*100:+.1f}%) | "
                f"{p['dte']}d left | {p['recommendation']}"
            )
        elif p.get("kind") == "stock":
            pos_lines.append(
                f"  {p['ticker']} stock | "
                f"P&L {p['pnl']:+.0f} ({p['pnl_pct']*100:+.1f}%) | "
                f"{p['recommendation']}"
            )

    total_pnl  = portfolio.get("total_pnl", 0)
    total_cost = portfolio.get("total_cost", 0)
    vix        = portfolio.get("vix", 0)

    prompt = f"""End-of-day position review:

{chr(10).join(pos_lines) if pos_lines else "No open positions."}

Portfolio: Cost ${total_cost:.0f} | P&L {total_pnl:+.0f} | VIX {vix:.1f}

Write a 3-4 sentence evening debrief. Cover:
1. How the portfolio is sitting overall
2. Any position that needs attention tomorrow (approaching take-profit, stop, or DTE warning)
3. One thing to prepare for tomorrow's open

Be direct. No padding."""

    try:
        msg = _get_client().messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"[Debrief unavailable: {e}]"
