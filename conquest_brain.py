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


def intelligence_brief(data: dict) -> tuple:
    """
    Generate the full Morning Intelligence Brief from collected market data.
    Outputs six narrative sections as a JSON object for reliable parsing.

    Args:
        data: dict from morning_brief.collect_brief_data()

    Returns:
        (sections_dict, discord_summary_str)
    """
    import json as _json

    snapshot = data.get("snapshot", {})
    fred     = data.get("fred", {})
    scan     = data.get("scan", [])
    paper    = data.get("paper_stats", {})
    sectors  = data.get("sector_rotation", [])

    # ── Build market data block ───────────────────────────────────────────────
    mkt_lines = []
    for key, label in [
        ("SPY",    "S&P 500 (SPY)"),
        ("QQQ",    "Nasdaq 100 (QQQ)"),
        ("DIA",    "Dow Jones (DIA)"),
        ("^VIX",   "VIX"),
        ("^TNX",   "10Y Yield"),
        ("^IRX",   "2Y Yield"),
        ("UUP",    "US Dollar Index"),
        ("GLD",    "Gold"),
        ("CL=F",   "WTI Crude"),
        ("HG=F",   "Copper"),
        ("HYG",    "HYG Credit"),
    ]:
        s = snapshot.get(key, {})
        if s:
            mkt_lines.append(
                f"  {label}: {s['price']} ({s['chg']:+.2f}% 1d, {s['ret5']:+.2f}% 5d)"
            )
    market_block = "\n".join(mkt_lines) or "  Market data unavailable"

    # ── Sector rotation block ─────────────────────────────────────────────────
    if sectors:
        sector_lines = [
            f"  {d['name']}: {d['ret5']:+.2f}% 5d ({d['chg']:+.2f}% 1d)"
            for d in sectors
        ]
        sector_block = "\n".join(sector_lines)
    else:
        sector_block = "  Sector data unavailable"

    # ── FRED block ────────────────────────────────────────────────────────────
    fred_parts = []
    for sid, r in fred.items():
        if r.get("error") or r.get("latest") is None:
            continue
        val = r["latest"]
        if sid == "GDPC1":
            qoq = r.get("qoq") or 0
            fred_parts.append(f"  Real GDP: ${val:,.0f}B ({qoq:+.1f}% ann.)")
        elif sid == "CPIAUCSL":
            yoy = r.get("yoy") or 0
            fred_parts.append(f"  CPI Index: {val:.1f} ({yoy:+.1f}% YoY)")
        elif sid == "FEDFUNDS":
            fred_parts.append(f"  Fed Funds Rate: {val:.2f}%")
        elif sid == "DGS10":
            fred_parts.append(f"  10Y Treasury (FRED): {val:.2f}%")
        elif sid == "T10Y2Y":
            status = "normal" if val >= 0 else "INVERTED"
            fred_parts.append(f"  Yield Curve (10Y-2Y): {val:+.2f}% ({status})")
        elif sid == "UNRATE":
            fred_parts.append(f"  Unemployment: {val:.1f}%")
        elif sid == "UMCSENT":
            fred_parts.append(f"  Consumer Sentiment: {val:.1f}")
    fred_block = "\n".join(fred_parts) or "  FRED data unavailable"

    # ── Signal scan block ─────────────────────────────────────────────────────
    entries = [r for r in scan if r.get("entry_signal") and not r.get("error")]
    crosses = [r for r in scan if r.get("macd_cross_up") and not r.get("entry_signal") and not r.get("error")]
    signal_lines = []
    for r in entries[:5]:
        signal_lines.append(
            f"  ENTRY: {r['ticker']} ${r['price']:.2f} "
            f"({r.get('today_chg_pct', 0):+.1f}%) "
            f"MTF {r['mtf_score']}/3 RSI {r['rsi']:.0f} HVR {r['hv_rank']:.0f}"
        )
    for r in crosses[:4]:
        signal_lines.append(
            f"  MACD CROSS: {r['ticker']} ${r['price']:.2f} MTF {r['mtf_score']}/3"
        )
    signal_block = (
        "\n".join(signal_lines) if signal_lines
        else f"  No active entry signals ({len(scan)} tickers scanned)."
    )

    # ── Paper trading block ───────────────────────────────────────────────────
    if paper.get("total_trades", 0) > 0:
        paper_block = (
            f"  Trades: {paper['total_trades']} "
            f"({paper.get('open_count', 0)} open, {paper.get('closed_count', 0)} closed)\n"
            f"  Win Rate: {paper.get('win_rate', 0) * 100:.1f}%  "
            f"Total P&L: ${paper.get('total_pnl', 0):+.2f}\n"
            f"  Sharpe: {paper.get('sharpe') or 'N/A'}  "
            f"Profit Factor: {paper.get('profit_factor') or 'N/A'}"
        )
    else:
        paper_block = "  No paper trade data yet."

    date_str = data.get("market_date", "today")

    prompt = f"""DATE: {date_str}

LIVE MARKET DATA:
{market_block}

SECTOR ROTATION — 5-DAY PERFORMANCE (best to worst):
{sector_block}

FEDERAL RESERVE / FRED MACRO DATA:
{fred_block}

QUANTITATIVE SIGNAL SCAN:
{signal_block}

AUTOMATED PAPER TRADING STATS:
{paper_block}

---

Generate a Morning Intelligence Brief for a quantitative trading desk.
Write like a senior macro analyst: specific, data-driven, flowing prose, no bullet points.
Every claim MUST reference actual numbers from the data above. No generic observations.

Output ONLY the following JSON object. No preamble, no markdown, just raw JSON:

{{
  "macro_regime": "4-5 sentences. Describe the current macro regime and cycle phase. What is the Fed narrative right now? What is the dominant force driving markets? Reference the yield curve reading, HYG credit conditions, and dollar movement from the data.",
  "overnight": "3-4 sentences. What do the price changes above tell us about overnight/pre-market action? Highlight the meaningful moves in yields, dollar, gold, crude, VIX. What does the data say about today's opening posture?",
  "data_vs_consensus": "4-5 sentences. Identify 2-3 specific points where the data above diverges from what consensus believes. Use the sector rotation numbers and signal data to support contrarian reads. Be specific about the mispricing.",
  "sector_positioning": "4-5 sentences. Use the 5-day sector rotation data. Name specific sectors with bullish and bearish conviction calls. Explain the rotation rationale and what is driving the money flows.",
  "portfolio_implications": "3-4 sentences. Translate the macro and sector picture into concrete trade structure guidance. What kind of trades (calls, puts, spreads, iron condors, stocks) make sense in this regime and why?",
  "what_to_watch": "3-4 sentences. Name 3-4 specific things to monitor today — exact levels or conditions that would change your view. Be actionable, not generic.",
  "discord_summary": "2-3 tight sentences covering the most critical takeaways. Under 400 characters total. No special formatting."
}}"""

    try:
        msg = _get_client().messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()

        # Strip markdown code fences if Claude wrapped the JSON
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        parsed   = _json.loads(raw)
        discord_summary = parsed.pop("discord_summary", "")
        return parsed, discord_summary

    except _json.JSONDecodeError:
        # JSON parsing failed — store the raw text so the page can still display it
        raw_text = locals().get("raw", "")
        return {"full_text": raw_text} if raw_text else {"error": "JSON parse failed"}, ""
    except Exception as e:
        return {"error": str(e)}, f"Brief unavailable: {e}"


def watchlist_thesis(ticker: str, data_block: str) -> dict:
    """
    Prompts 1 + 2 + 3 combined.
    Returns a dict with thesis, narrative_hook, fundamentals,
    price targets, conviction, waiting_for, entry_zone, hard_stop.

    All analysis is framed for STOCK BUYING (long equity) — not options.
    """
    import json as _json

    prompt = f"""{data_block}

---
Using the data above, generate a watchlist entry for {ticker}. Apply three analytical lenses.

IMPORTANT: This analysis is for BUYING SHARES (long equity position) ONLY.
Do NOT mention options, calls, puts, spreads, or any derivatives.
All trade parameters (entry, stop, targets) are for purchasing stock directly.

LENS 1 — NARRATIVE (what is driving the stock):
Cover the dominant narrative (social media, retail sentiment), the actual catalyst
(earnings/contract/policy — be specific with numbers), and the institutional view
(recent analyst target changes, upgrades/downgrades).
End with: "The stock is moving because [X], but [Y] is the part nobody is talking about."

LENS 2 — FUNDAMENTALS (what is it worth):
Is the stock trading above, at, or below its fundamental fair value?
Show the math using the valuation data. Compare forward P/E and P/Sales to sector averages.
Comment on balance sheet health and any dilution risk. One paragraph max.

LENS 3 — STOCK TRADE SETUP (where to buy, where to stop, where to sell):
Build a price target framework for buying shares. Show the math (multiple × EPS or revenue).
Name the specific buy zone, trim/sell levels, and the hard stop price where the thesis breaks.
All prices are stock prices, not option strikes.

Output ONLY this JSON — no preamble, no markdown:
{{
  "thesis": "2-3 sentences combining narrative and fundamental view. Direct and specific. No options language.",
  "narrative_hook": "The stock is moving because X, but Y is the part nobody is talking about.",
  "fundamentals": "One paragraph. Fair value assessment with math. Above/at/below?",
  "bear_target": "$XX — stock price if catalyst disappoints (3-6 months)",
  "base_target": "$XX — stock price if execution holds (6-12 months)",
  "bull_target": "$XX — stock price if everything works (12-18 months)",
  "entry_zone": "$XX–$XX (buy shares in this range)",
  "hard_stop": "$XX — close position if stock closes below this price",
  "conviction": "HIGH or MEDIUM or LOW",
  "waiting_for": "One specific, concrete condition before buying shares"
}}"""

    try:
        msg = _get_client().messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return _json.loads(raw)
    except _json.JSONDecodeError:
        return {"thesis": locals().get("raw", "Parse error"), "conviction": "LOW",
                "waiting_for": "N/A", "error": "json_parse"}
    except Exception as e:
        return {"thesis": f"Analysis failed: {e}", "conviction": "LOW", "waiting_for": "N/A"}


def watchlist_risks(ticker: str, data_block: str) -> dict:
    """
    Prompt 6 — skeptical 3-point risk assessment.
    Returns dict with 'risks' (summary) and 'risk_flags' (list of 3).
    """
    import json as _json

    prompt = f"""{data_block}

---
Act as a skeptic evaluating {ticker} as a stock to BUY AND HOLD.
This is for long equity only — not options trading.

3-point risk assessment for a stock buyer — be specific to the data above:
1. Downside risk: what could cause the stock price to fall significantly from here?
   Reference valuation metrics and any overextension in the data.
2. Business risk: revenue quality, customer concentration, or balance sheet concerns
   that could impair earnings — use actual numbers.
3. Competitive threats: who is taking market share and how fast — name the competitors.

Output ONLY this JSON:
{{
  "risks": "2-3 sentence overall bear thesis for a stock buyer. What could go wrong with buying shares here?",
  "risk_flags": [
    "Specific downside risk with numbers",
    "Specific business/revenue risk with numbers",
    "Specific competitive threat with names"
  ]
}}"""

    try:
        msg = _get_client().messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return _json.loads(raw)
    except Exception as e:
        return {"risks": f"Risk analysis failed: {e}", "risk_flags": []}


def watchlist_deep_dive_report(ticker: str, data_block: str) -> str:
    """
    Prompt 4 — full deep research report.
    Returns formatted text (4 sections).
    """
    prompt = f"""{data_block}

---
Generate a comprehensive Deep Research Report on {ticker}.

Cover these 4 areas in order:

1. BUSINESS MODEL
How exactly do they make money? Core product in plain English. Revenue streams.

2. MOAT AND COMPETITION
Top 3 competitors by name. Does {ticker} have a unique technological advantage,
patent, or network effect that competitors genuinely lack? Be honest about moat quality.

3. CATALYST
Upcoming product launches, regulatory approvals, or partnerships in the next 12 months.
Be specific about dates and magnitude if known.

4. ASYMMETRY CHECK
What is the low valuation floor vs the high growth ceiling?
Why is this a good risk/reward — or why not?

Be specific throughout. Reference the data above. 4-6 sentences per section."""

    try:
        msg = _get_client().messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"Deep dive failed: {e}"


def watchlist_screener(tickers_blocks: list) -> list:
    """
    Prompt 5 adapted — screen multiple tickers for value vs growth.
    tickers_blocks: list of (ticker, data_block) tuples
    Returns list of dicts sorted by Value/Growth Score.
    """
    import json as _json

    sections = "\n\n".join(
        f"=== {ticker} ===\n{block[:800]}"
        for ticker, block in tickers_blocks
    )

    prompt = f"""You are screening these stocks for fundamental value vs growth quality.

{sections}

---
For each stock, calculate the Value/Growth Score = P/S TTM ÷ revenue growth %.
Lower score = more growth per valuation dollar (better).

Also flag: any stock with P/E below the typical sector average AND positive revenue growth
qualifies as potentially undervalued.

Output ONLY a JSON array sorted by score ascending (best first):
[
  {{
    "ticker": "X",
    "score": 1.23,
    "ps_ttm": 4.5,
    "rev_growth_pct": 22.0,
    "verdict": "UNDERVALUED",
    "reason": "One specific sentence explaining why."
  }}
]

Use "UNDERVALUED", "FAIRLY VALUED", or "OVERVALUED" for verdict.
If data is missing for score calculation, estimate and note it."""

    try:
        msg = _get_client().messages.create(
            model="claude-sonnet-4-5",
            max_tokens=800,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return _json.loads(raw)
    except Exception as e:
        return []


def paper_evening_debrief(stats: dict, today_trades: list, all_time_pnl: float) -> str:
    """
    Claude-narrated end-of-day wrap for automated paper trading.
    Called at 4:05 PM ET after the numeric EOD summary is posted.

    stats        : get_paper_stats() dict
    today_trades : list of trade dicts closed today
    all_time_pnl : running total P&L
    """
    today_pnl  = sum(t.get("pnl", 0) for t in today_trades)
    win_today  = sum(1 for t in today_trades if t.get("pnl", 0) >= 0)
    loss_today = len(today_trades) - win_today

    trade_lines = []
    for t in sorted(today_trades, key=lambda x: x.get("pnl", 0), reverse=True)[:6]:
        pnl    = t.get("pnl", 0)
        reason = t.get("close_reason", "").replace("_", " ")
        trade_lines.append(
            f"  {t['ticker']} {t['trade_type'].replace('_',' ')} → "
            f"${pnl:+.2f} ({t.get('pnl_pct', 0)*100:+.1f}%) via {reason}"
        )

    trades_block = "\n".join(trade_lines) if trade_lines else "  No trades closed today."

    prompt = f"""End-of-day paper trading summary:

TODAY:
  Trades closed: {len(today_trades)} ({win_today} wins / {loss_today} losses)
  Today P&L:     ${today_pnl:+.2f}

TOP TRADES TODAY:
{trades_block}

ALL-TIME STATS:
  Total closed: {stats.get('closed_count', 0)}
  Win rate:     {stats.get('win_rate', 0)*100:.1f}%
  All-time P&L: ${all_time_pnl:+.2f}
  Sharpe ratio: {stats.get('sharpe') or 'N/A'}
  Profit factor:{stats.get('profit_factor') or 'N/A'}

Write a 3-4 sentence end-of-day debrief for the paper trading simulation.
Cover: (1) how today went — was it a clean day or messy exits, (2) any pattern worth
noting in what worked vs what didn't, (3) one forward-looking thought for tomorrow.
Be direct and analytical. Reference actual numbers."""

    try:
        msg = _get_client().messages.create(
            model="claude-haiku-4-5",
            max_tokens=250,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"[Debrief unavailable: {e}]"


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
