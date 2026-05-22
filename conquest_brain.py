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


SYSTEM_PROMPT = """You are the Conquest intelligence engine — the senior macro analyst and intelligence officer
for a quantitative trading desk. You have deep knowledge of macro economics, monetary policy,
technical analysis, options strategy, credit markets, and global capital flows.

You think like Michael Burry: find the signal in the noise, read the leading indicators that
the consensus is ignoring, and have the conviction to call the divergence clearly.
You write like a managing director at a top hedge fund giving the morning session briefing —
direct, specific, data-anchored, and actionable.

Rules:
- Be direct and specific. No fluff, no generic statements.
- Every claim must reference actual numbers from the data provided.
- Never say "this is not financial advice" — the user already knows.
- When signals are weak, say so plainly. Don't hype mediocre setups.
- When signals are strong, show conviction. Don't hedge excessively.
- Write in full prose paragraphs, not bullet points, for briefings.
- Name specific sectors, tickers, levels, and spreads when supported by the data.
- The goal is to give a trader information they cannot get from reading a headline.
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

You are the senior macro intelligence officer at a top-tier quantitative hedge fund.
Write the morning session brief. This goes to traders who will be actively managing
positions in the next six hours. They need depth, conviction, and specificity.

WRITING STANDARDS:
- Every claim must be anchored to an actual number from the data above.
- No bullet points. Full flowing prose paragraphs.
- Do not hedge everything. When the data supports conviction, show conviction.
- When something is uncertain or ambiguous, say exactly that and why.
- Name specific sectors, tickers, levels, spreads when the data supports it.
- Write like the example of great macro analysis: Michael Burry finding the signal
  in the noise, not a generic summary bot.
- Each section should be 4-8 substantive sentences — not thin summaries.
- No generic filler like "markets are complex" or "investors should be cautious".
  Every sentence must add information that wasn't in the previous one.

Output ONLY the following JSON object. No preamble, no markdown, just raw JSON:

{{
  "macro_regime": "The current macro regime assessment. 5-7 sentences. What cycle phase are we in? What is the Fed's actual position vs. what markets are pricing? What is the dominant force right now — is it rates, liquidity, earnings, geopolitics? What is the key tension or divergence in the current regime? Reference the yield curve spread, HYG credit spread direction, dollar trend, and VIX level. What is the consensus getting wrong that the data reveals?",

  "overnight": "Overnight and pre-market analysis. 4-6 sentences. Walk through the meaningful moves in the data: what moved, how much, and what it means directionally. Yields, dollar, gold, crude, VIX — which are signaling risk-on or risk-off and are they confirming each other or diverging? Is pre-market equity positioning low-conviction or directional? What is the opening posture today based on this data?",

  "data_vs_consensus": "What the data is telling you vs. what consensus believes. 5-8 sentences. Identify 2-3 specific divergences between what the numbers show and what the market is pricing. Use the sector rotation data — which sectors are getting flows that don't match the consensus narrative? Are credit conditions (HYG) confirming equity optimism or quietly warning? Where is the specific mispricing and what is the trade that follows from it?",

  "sector_positioning": "Sector positioning rationale. 5-7 sentences. Use the exact 5-day sector rotation numbers. Name the top 2-3 performing sectors and the specific thesis for why money is flowing there — is it structural or rotational? Name the bottom 2-3 and whether this is weakness to fade or trend to follow. What is the highest-conviction sector call today given the macro backdrop and the rotation data?",

  "portfolio_implications": "Portfolio implications and trade posture. 4-6 sentences. Translate the macro picture into specific trade structure guidance. What is the net directional bias today — long beta, short beta, or neutral? What types of setups have the best risk/reward in this regime? If there are active entry signals from the scan, what is the conviction level given the macro backdrop? What is the single most important thing to get right in positioning today?",

  "what_to_watch": "What to watch today — specific catalysts and level triggers. 4-6 sentences. Name 3-4 precise things to monitor: specific price levels, spread moves, Fed speaker events, data prints, or sector behaviors that would confirm or invalidate the thesis. For each, state what it means if it happens. Be actionable — a trader should be able to write these down as a checklist.",

  "discord_summary": "3 tight sentences. The single most important macro call, the highest-conviction trade setup, and the key risk. Under 500 characters total. No special formatting."
}}"""

    try:
        msg = _get_client().messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4500,
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

    Batches 15 tickers at a time to avoid token overflow.
    """
    import json as _json

    BATCH_SIZE = 15

    def _screen_batch(batch):
        sections = "\n\n".join(
            f"=== {ticker} ===\n{block[:600]}"
            for ticker, block in batch
        )
        prompt = f"""You are screening these {len(batch)} stocks for fundamental value vs growth quality.

{sections}

---
For each stock, calculate the Value/Growth Score = P/S TTM ÷ revenue growth %.
Lower score = more growth per valuation dollar (better).

Also flag: any stock with P/E below typical sector average AND positive revenue growth
as potentially undervalued.

Output ONLY a raw JSON array (no markdown, no fences) with ALL {len(batch)} tickers:
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
If data is missing, estimate from context and note it in the reason field.
You MUST include every ticker — do not skip any."""

        msg = _get_client().messages.create(
            model="claude-haiku-4-5",
            max_tokens=2500,
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

    all_results = []
    for i in range(0, len(tickers_blocks), BATCH_SIZE):
        batch = tickers_blocks[i : i + BATCH_SIZE]
        try:
            results = _screen_batch(batch)
            all_results.extend(results)
        except Exception as e:
            # Partial failure — add error entries so we know which batch failed
            print(f"[Screener] Batch {i//BATCH_SIZE + 1} failed: {e}")
            for ticker, _ in batch:
                all_results.append({
                    "ticker": ticker,
                    "score": None,
                    "verdict": "ERROR",
                    "reason": f"Batch error: {e}"
                })

    # Sort: valid scores first (ascending), errors last
    all_results.sort(key=lambda x: (x.get("score") is None, x.get("score") or 9999))
    return all_results


def generate_trade_reasonings(trades_and_scans: list) -> dict:
    """
    Generate entry reasoning for a batch of new paper trades.
    trades_and_scans: list of (trade_dict, scan_dict) tuples
    Returns dict mapping trade_id → reasoning string.

    Uses Haiku for speed/cost on a batch of up to 10 trades.
    """
    import json as _json

    if not trades_and_scans:
        return {}

    lines = []
    for i, (trade, scan) in enumerate(trades_and_scans):
        tt       = trade["trade_type"].replace("_", " ").upper()
        ticker   = trade["ticker"]
        price    = scan.get("price", 0)
        mtf      = scan.get("mtf_score", 0)
        daily    = scan.get("daily", "?")
        weekly   = scan.get("weekly", "?")
        monthly  = scan.get("monthly", "?")
        rsi      = scan.get("rsi", 50)
        adx      = scan.get("adx", 0)
        hvr      = scan.get("hv_rank", 0)
        entry    = scan.get("entry_signal", False)
        sqz      = scan.get("sqz_fired", False)
        macd     = scan.get("macd_cross_up", False)

        # Trade-specific details
        extra = ""
        if trade["trade_type"] in ("call_spread", "put_spread"):
            extra = f"Spread: ${trade.get('long_strike','?')}/${trade.get('short_strike','?')}  Debit: ${trade.get('entry_net_debit','?')}  Max profit: ${trade.get('max_profit','?')}"
        elif trade["trade_type"] in ("long_call", "long_put"):
            extra = f"Strike: ${trade.get('strike','?')}  Premium: ${trade.get('entry_option_price','?')}"
        elif trade["trade_type"] == "iron_condor":
            extra = f"Wings: ${trade.get('long_put_k','?')}/${trade.get('short_put_k','?')}/{trade.get('short_call_k','?')}/{trade.get('long_call_k','?')}  Credit: ${trade.get('entry_net_credit','?')}"
        elif trade["trade_type"] in ("stock_long", "stock_short"):
            extra = f"Entry: ${trade.get('entry_price','?')}  Shares: {trade.get('shares','?')}"

        lines.append(
            f"TRADE {i+1}: {ticker} {tt}\n"
            f"  Price: ${price:.2f}  MTF: {mtf}/3 (M:{monthly}/W:{weekly}/D:{daily})\n"
            f"  RSI: {rsi:.0f}  ADX: {adx:.0f}  HV Rank: {hvr:.0%}\n"
            f"  Signals: entry={entry}  squeeze_fired={sqz}  macd_cross={macd}\n"
            f"  {extra}"
        )

    trades_block = "\n\n".join(lines)

    prompt = f"""You are narrating the entry reasoning for {len(trades_and_scans)} new paper trades opened today.

{trades_block}

---
For each trade, write 2-3 sentences explaining:
1. Why this specific ticker was selected (what the signals showed)
2. Why this trade structure was chosen over alternatives (why call spread vs long call, why iron condor vs directional, etc.)
3. What the thesis is — what needs to happen for this trade to win

Be specific — reference the actual numbers (RSI, ADX, MTF score, strikes).
Write like a trader logging their rationale, not a textbook.

Output ONLY raw JSON — no markdown, no fences:
[
  {{"id": 1, "reasoning": "2-3 sentence entry rationale here."}},
  {{"id": 2, "reasoning": "..."}},
  ...
]"""

    try:
        msg = _get_client().messages.create(
            model="claude-haiku-4-5",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = _json.loads(raw)
        return {
            trades_and_scans[item["id"] - 1][0]["id"]: item["reasoning"]
            for item in parsed
            if 1 <= item["id"] <= len(trades_and_scans)
        }
    except Exception as e:
        print(f"[Brain] generate_trade_reasonings error: {e}")
        return {}


def generate_close_reasoning(trade: dict, close_reason: str, current_price: float) -> str:
    """
    Generate a close/stop reasoning for a single trade being closed.
    close_reason: 'profit_target' | 'stop_loss' | 'max_hold'
    Returns a 2-3 sentence string.
    """
    tt      = trade["trade_type"].replace("_", " ").upper()
    ticker  = trade["ticker"]
    pnl     = trade.get("pnl", 0)
    pnl_pct = trade.get("pnl_pct", 0) * 100
    days    = trade.get("days_held", 0)
    entry   = trade.get("entry_price") or trade.get("entry_net_debit") or trade.get("entry_net_credit", 0)

    reason_labels = {
        "profit_target": "hit the profit target",
        "stop_loss":     "hit the stop loss",
        "max_hold":      "reached the maximum hold period",
        "manual":        "manually closed",
    }
    reason_text = reason_labels.get(close_reason, close_reason)

    # Trade-specific close context
    if trade["trade_type"] in ("call_spread", "put_spread"):
        close_context = f"Spread closed at net value ${trade.get('current_net_value', 0):.2f} vs entry debit ${entry:.2f}. Max profit was ${trade.get('max_profit', 0):.2f}."
    elif trade["trade_type"] == "iron_condor":
        close_context = f"Condor closed at net value ${trade.get('current_net_value', 0):.2f} vs entry credit ${entry:.2f}."
    elif trade["trade_type"] in ("long_call", "long_put"):
        close_context = f"Option closed at ${trade.get('current_option_price', 0):.2f} vs entry ${entry:.2f}."
    else:
        close_context = f"Stock closed at ${current_price:.2f} vs entry ${entry:.2f}."

    prompt = f"""A paper trade just closed. Write 2-3 sentences explaining what happened and what to learn from it.

TRADE: {ticker} {tt}
CLOSE REASON: {reason_text}
P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)
Days held: {days}
{close_context}
MTF at entry: {trade.get('mtf_score', '?')}/3
RSI at entry: {trade.get('rsi_entry', '?')}

Cover: what the outcome was, why the close trigger fired, and what the key lesson is (did the trade work as planned, did it stop out before the thesis played out, or did it max-hold because the move stalled).
Be direct and specific. No disclaimers."""

    try:
        msg = _get_client().messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"[Reasoning unavailable: {e}]"


def send_eod_email(stats: dict, closed_today: list, debrief_text: str) -> bool:
    """
    Send an HTML end-of-day summary email via Gmail SMTP.

    Required Railway env vars:
      GMAIL_USER         — sender address (e.g. you@gmail.com)
      GMAIL_APP_PASSWORD — 16-char Gmail App Password (not your account password)
      ALERT_EMAIL        — recipient address (can be same as GMAIL_USER)

    Returns True if sent successfully, False otherwise.
    """
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from datetime import date

    gmail_user = os.getenv("GMAIL_USER", "")
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD", "")
    alert_to   = os.getenv("ALERT_EMAIL", gmail_user)

    if not gmail_user or not gmail_pass:
        print("[Email] GMAIL_USER / GMAIL_APP_PASSWORD not set — skipping EOD email.")
        return False

    today       = date.today().strftime("%B %d, %Y")
    total_pnl   = stats.get("total_pnl", 0)
    win_rate    = stats.get("win_rate", 0) * 100
    closed_cnt  = stats.get("closed_count", 0)
    open_cnt    = stats.get("open_count", 0)
    sharpe      = stats.get("sharpe") or "N/A"
    pf          = stats.get("profit_factor") or "N/A"

    today_pnl   = sum(t.get("pnl", 0) for t in closed_today)
    pnl_color   = "#16a34a" if today_pnl >= 0 else "#dc2626"
    total_color = "#16a34a" if total_pnl >= 0 else "#dc2626"

    # Build trade rows
    rows = ""
    for t in sorted(closed_today, key=lambda x: x.get("pnl", 0), reverse=True):
        pnl   = t.get("pnl", 0)
        color = "#16a34a" if pnl >= 0 else "#dc2626"
        rows += (
            f"<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #e5e7eb'>{t['ticker']}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #e5e7eb'>{t['trade_type'].replace('_',' ')}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #e5e7eb;color:{color};font-weight:600'>"
            f"${pnl:+.2f}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #e5e7eb'>"
            f"{t.get('close_reason','').replace('_',' ')}</td>"
            f"</tr>"
        )

    trades_table = f"""
    <table style='width:100%;border-collapse:collapse;font-size:13px;margin-top:8px'>
      <thead>
        <tr style='background:#f3f4f6'>
          <th style='padding:8px 10px;text-align:left'>Ticker</th>
          <th style='padding:8px 10px;text-align:left'>Type</th>
          <th style='padding:8px 10px;text-align:left'>P&amp;L</th>
          <th style='padding:8px 10px;text-align:left'>Closed By</th>
        </tr>
      </thead>
      <tbody>{rows if rows else "<tr><td colspan='4' style='padding:8px 10px;color:#6b7280'>No trades closed today.</td></tr>"}</tbody>
    </table>""" if closed_today else "<p style='color:#6b7280;font-size:13px'>No trades closed today.</p>"

    debrief_html = debrief_text.replace("\n", "<br>") if debrief_text else ""

    html = f"""
<!DOCTYPE html>
<html>
<body style='font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f9fafb;margin:0;padding:20px'>
  <div style='max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)'>

    <!-- Header -->
    <div style='background:#0f172a;padding:24px 28px'>
      <h1 style='color:#fff;margin:0;font-size:20px;font-weight:700'>📈 Conquest Trading</h1>
      <p style='color:#94a3b8;margin:4px 0 0;font-size:13px'>End-of-Day Summary — {today}</p>
    </div>

    <!-- Today snapshot -->
    <div style='padding:24px 28px;border-bottom:1px solid #e5e7eb'>
      <h2 style='margin:0 0 16px;font-size:15px;color:#374151;text-transform:uppercase;letter-spacing:.05em'>Today</h2>
      <div style='display:flex;gap:24px;flex-wrap:wrap'>
        <div>
          <div style='font-size:11px;color:#6b7280;margin-bottom:2px'>TODAY P&amp;L</div>
          <div style='font-size:26px;font-weight:700;color:{pnl_color}'>${today_pnl:+.2f}</div>
        </div>
        <div>
          <div style='font-size:11px;color:#6b7280;margin-bottom:2px'>TRADES CLOSED</div>
          <div style='font-size:26px;font-weight:700;color:#111827'>{len(closed_today)}</div>
        </div>
      </div>
      {trades_table}
    </div>

    <!-- All-time stats -->
    <div style='padding:24px 28px;border-bottom:1px solid #e5e7eb'>
      <h2 style='margin:0 0 16px;font-size:15px;color:#374151;text-transform:uppercase;letter-spacing:.05em'>All-Time</h2>
      <div style='display:grid;grid-template-columns:repeat(3,1fr);gap:16px'>
        <div style='background:#f8fafc;border-radius:8px;padding:14px'>
          <div style='font-size:11px;color:#6b7280'>TOTAL P&amp;L</div>
          <div style='font-size:20px;font-weight:700;color:{total_color}'>${total_pnl:+.2f}</div>
        </div>
        <div style='background:#f8fafc;border-radius:8px;padding:14px'>
          <div style='font-size:11px;color:#6b7280'>WIN RATE</div>
          <div style='font-size:20px;font-weight:700;color:#111827'>{win_rate:.1f}%</div>
        </div>
        <div style='background:#f8fafc;border-radius:8px;padding:14px'>
          <div style='font-size:11px;color:#6b7280'>CLOSED / OPEN</div>
          <div style='font-size:20px;font-weight:700;color:#111827'>{closed_cnt} / {open_cnt}</div>
        </div>
        <div style='background:#f8fafc;border-radius:8px;padding:14px'>
          <div style='font-size:11px;color:#6b7280'>SHARPE</div>
          <div style='font-size:20px;font-weight:700;color:#111827'>{sharpe}</div>
        </div>
        <div style='background:#f8fafc;border-radius:8px;padding:14px'>
          <div style='font-size:11px;color:#6b7280'>PROFIT FACTOR</div>
          <div style='font-size:20px;font-weight:700;color:#111827'>{pf}</div>
        </div>
      </div>
    </div>

    <!-- Claude debrief -->
    {"<div style='padding:24px 28px;border-bottom:1px solid #e5e7eb'><h2 style='margin:0 0 12px;font-size:15px;color:#374151;text-transform:uppercase;letter-spacing:.05em'>Wolf's Take</h2><p style='font-size:14px;line-height:1.7;color:#374151;margin:0'>" + debrief_html + "</p></div>" if debrief_html else ""}

    <!-- Footer -->
    <div style='padding:16px 28px;background:#f8fafc'>
      <p style='margin:0;font-size:12px;color:#9ca3af'>Conquest Trading · Automated Paper Trading Engine · conquest-trading.up.railway.app</p>
    </div>
  </div>
</body>
</html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Conquest EOD — {today}  |  Today: ${today_pnl:+.2f}  |  All-time: ${total_pnl:+.2f}"
        msg["From"]    = gmail_user
        msg["To"]      = alert_to
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, alert_to, msg.as_string())

        print(f"[Email] EOD summary sent to {alert_to}")
        return True

    except Exception as e:
        print(f"[Email] send_eod_email failed: {e}")
        return False


def paper_evening_debrief(stats: dict, today_trades: list, all_time_pnl: float) -> str:
    """
    Claude-narrated end-of-day wrap for automated paper trading.
    Called at 4:05 PM ET after the numeric EOD summary is posted.

    stats        : get_paper_stats() dict  — includes open_trades list
    today_trades : list of trade dicts closed today
    all_time_pnl : running total P&L
    """
    from datetime import date as _date
    today_pnl  = sum(t.get("pnl", 0) for t in today_trades)
    win_today  = sum(1 for t in today_trades if t.get("pnl", 0) >= 0)
    loss_today = len(today_trades) - win_today

    # ── Closed trades block ───────────────────────────────────────────────────
    closed_lines = []
    for t in sorted(today_trades, key=lambda x: x.get("pnl", 0), reverse=True)[:6]:
        pnl    = t.get("pnl", 0)
        reason = t.get("close_reason", "?").replace("_", " ")
        closed_lines.append(
            f"  {t['ticker']} {t['trade_type'].replace('_',' ')} → "
            f"${pnl:+.2f} ({t.get('pnl_pct', 0)*100:+.1f}%) via {reason}"
        )
    closed_block = "\n".join(closed_lines) if closed_lines else "  No trades closed today."

    # ── Still-open positions block ────────────────────────────────────────────
    try:
        from paper_trader import STK_PROFIT, STK_STOP, OPT_PROFIT, OPT_STOP
    except Exception:
        STK_PROFIT, STK_STOP, OPT_PROFIT, OPT_STOP = 0.05, -0.03, 0.50, -0.75

    open_trades = stats.get("open_trades", [])
    open_lines  = []
    for t in open_trades:
        ticker     = t.get("ticker", "?")
        ttype      = t.get("trade_type", "?").replace("_", " ")
        pnl        = t.get("pnl", 0) or 0
        pnl_pct    = (t.get("pnl_pct", 0) or 0) * 100
        days_held  = t.get("days_held", 0) or 0
        days_left  = max(0, 5 - days_held)
        entry_r    = t.get("reasoning", "")
        cost       = t.get("cost_basis", 0) or 0
        if cost > 0:
            dist_profit = (STK_PROFIT if "stock" in ttype else OPT_PROFIT) * 100
            dist_stop   = abs(STK_STOP if "stock" in ttype else OPT_STOP) * 100
        else:
            dist_profit = dist_stop = 0

        entry_snippet = (entry_r[:120] + "…") if len(entry_r) > 120 else entry_r
        open_lines.append(
            f"  {ticker} {ttype} | P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%) | "
            f"Day {days_held}/5 ({days_left}d left) | "
            f"Original thesis: {entry_snippet or 'not recorded'}"
        )

    open_block = "\n".join(open_lines) if open_lines else "  No positions currently open."

    prompt = f"""End-of-day paper trading summary — {_date.today().strftime('%B %d, %Y')}

═══ TODAY'S CLOSED TRADES ═══
Closed: {len(today_trades)} trades  ({win_today}W / {loss_today}L)  |  Today P&L: ${today_pnl:+.2f}

{closed_block}

═══ POSITIONS STILL OPEN OVERNIGHT ═══
{open_block}

═══ ALL-TIME STATS ═══
  Closed trades: {stats.get('closed_count', 0)}   Win rate: {stats.get('win_rate', 0)*100:.1f}%
  All-time P&L:  ${all_time_pnl:+.2f}   Sharpe: {stats.get('sharpe') or 'N/A'}   PF: {stats.get('profit_factor') or 'N/A'}

Write a sharp end-of-day debrief covering THREE sections:

1. TODAY'S CLOSE — How did the day go? Were exits clean or forced? Any patterns in what worked vs what didn't? Reference actual numbers.

2. OPEN POSITIONS REVIEW — For EACH open position above, give one sentence: where it stands right now, why the system kept it (it hasn't hit its target or stop yet), and what to watch tomorrow. Be specific — use the ticker name and P&L.

3. TOMORROW'S OUTLOOK — One forward-looking thought based on what's still on the books and what today's action revealed.

Be direct and analytical. No fluff. Reference tickers by name."""

    try:
        msg = _get_client().messages.create(
            model="claude-haiku-4-5",
            max_tokens=500,
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


# ══════════════════════════════════════════════════════════════════════════════
# FULL STOCK ANALYSIS — 5-Dimension Deep Intelligence Report
# ══════════════════════════════════════════════════════════════════════════════

def full_stock_analysis(ticker: str) -> dict:
    """
    5-dimension deep analysis on any ticker:
      1. Valuation      — P/E, P/S, P/B, EV/EBITDA, PEG vs sector
      2. Technicals     — MTF score, RSI, ADX, MACD, squeeze, levels
      3. Catalysts      — earnings, analyst changes, guidance, sector
      4. Risk           — HV, beta, drawdown, Kelly sizing
      5. Options Flow   — P/C ratio, top OI, IV pct, unusual activity
      6. Synthesis      — overall verdict, entry, stop, target

    Returns a structured dict. All heavy lifting in one Claude Sonnet call.
    Suitable for Discord !deepdive, web dashboard, and dev session analysis.
    """
    import json as _json

    # ── 1. Fetch raw data ────────────────────────────────────────────────────
    try:
        import yfinance as yf
        import numpy as np

        tk   = yf.Ticker(ticker)
        info = tk.info or {}
        hist = tk.history(period="6mo", interval="1d", auto_adjust=True)
    except Exception as e:
        return {"error": f"Data fetch failed: {e}", "ticker": ticker}

    # Live price
    price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
    if not price and not hist.empty:
        price = float(hist["Close"].iloc[-1])

    # Valuation metrics
    val_metrics = {
        "forward_pe":     info.get("forwardPE"),
        "trailing_pe":    info.get("trailingPE"),
        "price_to_sales": info.get("priceToSalesTrailing12Months"),
        "price_to_book":  info.get("priceToBook"),
        "ev_to_ebitda":   info.get("enterpriseToEbitda"),
        "peg_ratio":      info.get("pegRatio"),
        "sector":         info.get("sector", "Unknown"),
        "market_cap":     info.get("marketCap"),
        "revenue_growth": info.get("revenueGrowth"),
        "earnings_growth":info.get("earningsGrowth"),
    }

    # Technicals from scanner
    scan = {}
    try:
        from alerts.scanner import scan_ticker
        scan = scan_ticker(ticker) or {}
    except Exception:
        pass

    # Historical volatility
    hv30 = hv60 = hv90 = beta = max_dd = 0.0
    try:
        if len(hist) >= 20:
            rets  = hist["Close"].pct_change().dropna()
            hv30  = float(rets.tail(21).std() * (252**0.5))
            hv60  = float(rets.tail(42).std() * (252**0.5))
            hv90  = float(rets.tail(63).std() * (252**0.5))
            beta  = float(info.get("beta", 0) or 0)
            # Max drawdown (6-month)
            roll_max = hist["Close"].cummax()
            dd       = (hist["Close"] - roll_max) / roll_max
            max_dd   = float(dd.min())
    except Exception:
        pass

    # Kelly position size suggestion
    kelly_pct = 0.0
    try:
        win_rate = 0.55   # conservative assumption
        avg_win  = abs(STK_PROFIT if "STK_PROFIT" in dir() else 0.05)
        avg_loss = abs(STK_STOP   if "STK_STOP"   in dir() else 0.03)
        kelly_pct = max(0, (win_rate / avg_loss) - ((1 - win_rate) / avg_win)) * 0.25
    except Exception:
        kelly_pct = 0.02

    # 52-week range
    w52_low  = info.get("fiftyTwoWeekLow",  price * 0.7)
    w52_high = info.get("fiftyTwoWeekHigh", price * 1.3)
    pct_from_high = (price - w52_high) / w52_high if w52_high else 0

    # Next earnings
    earnings_date = "Unknown"
    try:
        cal = tk.calendar
        if cal is not None and not (hasattr(cal, "empty") and cal.empty):
            if hasattr(cal, "get"):
                ed = cal.get("Earnings Date")
                if ed and len(ed) > 0:
                    earnings_date = str(ed[0])[:10]
    except Exception:
        pass

    # Analyst consensus
    analyst_reco  = info.get("recommendationKey", "none").upper()
    analyst_count = info.get("numberOfAnalystOpinions", 0)
    target_mean   = info.get("targetMeanPrice", 0)
    target_upside = ((target_mean - price) / price * 100) if target_mean and price else 0

    # ── 2. Options flow ──────────────────────────────────────────────────────
    pc_ratio = call_oi = put_oi = iv_avg = 0.0
    top_calls_str = top_puts_str = "N/A"
    try:
        exps = tk.options
        if exps:
            chain  = tk.option_chain(exps[0])
            calls  = chain.calls
            puts   = chain.puts
            call_oi = float(calls["openInterest"].fillna(0).sum())
            put_oi  = float(puts["openInterest"].fillna(0).sum())
            pc_ratio = round(put_oi / max(call_oi, 1), 3)
            iv_avg   = float(calls["impliedVolatility"].fillna(0).mean())

            top3c = calls.nlargest(3, "openInterest")[
                ["strike","openInterest","impliedVolatility"]
            ].to_dict("records")
            top3p = puts.nlargest(3, "openInterest")[
                ["strike","openInterest","impliedVolatility"]
            ].to_dict("records")

            top_calls_str = " | ".join(
                f"${r['strike']:.0f} ({int(r['openInterest']):,} OI, IV {r['impliedVolatility']:.0%})"
                for r in top3c
            )
            top_puts_str = " | ".join(
                f"${r['strike']:.0f} ({int(r['openInterest']):,} OI, IV {r['impliedVolatility']:.0%})"
                for r in top3p
            )
    except Exception:
        pass

    # ── 3. Build analysis prompt ─────────────────────────────────────────────
    mtf_score = scan.get("mtf_score", 0)
    rsi       = scan.get("rsi", 50)
    adx       = scan.get("adx", 20)
    hv_rank   = scan.get("hv_rank", 50)
    daily     = scan.get("daily",   "?")
    weekly    = scan.get("weekly",  "?")
    monthly   = scan.get("monthly", "?")
    sqz_fired = scan.get("sqz_fired", False)
    sqz_mom   = scan.get("sqz_momentum", 0.0)
    macd_x    = scan.get("macd_cross_up", False)
    entry_sig = scan.get("entry_signal", False)

    prompt = f"""You are conducting a full 5-dimension investment analysis on {ticker}.
Current price: ${price:.2f}

━━ VALUATION DATA ━━
Forward P/E:     {val_metrics['forward_pe']}
Trailing P/E:    {val_metrics['trailing_pe']}
P/S (TTM):       {val_metrics['price_to_sales']}
P/B:             {val_metrics['price_to_book']}
EV/EBITDA:       {val_metrics['ev_to_ebitda']}
PEG Ratio:       {val_metrics['peg_ratio']}
Revenue Growth:  {val_metrics['revenue_growth']}
Earnings Growth: {val_metrics['earnings_growth']}
Market Cap:      ${(val_metrics['market_cap'] or 0)/1e9:.1f}B
Sector:          {val_metrics['sector']}

━━ TECHNICAL DATA ━━
MTF Score:       {mtf_score}/3 (Monthly:{monthly} / Weekly:{weekly} / Daily:{daily})
RSI (14):        {rsi:.1f}
ADX:             {adx:.1f}
HV Rank:         {hv_rank:.0f}/100
Squeeze Fired:   {sqz_fired}  (Momentum: {sqz_mom:+.3f})
MACD Cross Up:   {macd_x}
Entry Signal:    {entry_sig}
52W Low/High:    ${w52_low:.2f} / ${w52_high:.2f}  ({pct_from_high:+.1%} from high)

━━ CATALYST DATA ━━
Next Earnings:   {earnings_date}
Analyst Reco:    {analyst_reco} ({analyst_count} analysts)
Mean Price Target: ${target_mean:.2f} ({target_upside:+.1f}% upside)

━━ RISK DATA ━━
HV30/60/90:      {hv30:.1%} / {hv60:.1%} / {hv90:.1%}
Beta:            {beta:.2f}
Max Drawdown (6mo): {max_dd:.1%}
Kelly Size:      {kelly_pct:.1%} of portfolio

━━ OPTIONS FLOW DATA ━━
Put/Call Ratio:  {pc_ratio:.3f}  (call OI {call_oi:,.0f} / put OI {put_oi:,.0f})
Avg Call IV:     {iv_avg:.1%}
Top Call Strikes: {top_calls_str}
Top Put Strikes:  {top_puts_str}

---

Analyze this stock across all 5 dimensions. Be specific and reference the actual numbers.
Write like a senior analyst at a top hedge fund, not a generic stock report generator.
When the data is ambiguous, say so. When conviction is clear, show it.

Output ONLY this JSON — no markdown, no fences:
{{
  "valuation": {{
    "verdict": "OVERVALUED|FAIRLY_VALUED|UNDERVALUED",
    "analysis": "3-4 sentences. Reference the actual multiples. Compare to sector. Is the growth rate justifying the premium or not?"
  }},
  "technicals": {{
    "verdict": "BULLISH|BEARISH|NEUTRAL",
    "signals": {{
      "trend": "MTF score interpretation",
      "momentum": "RSI + MACD interpretation",
      "strength": "ADX interpretation",
      "squeeze": "squeeze fired or not"
    }},
    "analysis": "3-4 sentences. Key levels, what needs to happen to confirm, what breaks the setup."
  }},
  "catalysts": {{
    "verdict": "POSITIVE|NEGATIVE|MIXED",
    "analysis": "3-4 sentences. Earnings timing, analyst positioning, what the consensus is getting right or wrong."
  }},
  "risk": {{
    "verdict": "HIGH|MEDIUM|LOW",
    "analysis": "3-4 sentences. Volatility profile, beta context, max drawdown concern, position sizing guidance."
  }},
  "options_flow": {{
    "verdict": "BULLISH|BEARISH|NEUTRAL",
    "analysis": "3-4 sentences. P/C ratio interpretation, where the big bets are positioned, what institutional options activity signals about the next move."
  }},
  "synthesis": {{
    "overall": "BUY|WATCH|HOLD|AVOID",
    "conviction": "HIGH|MEDIUM|LOW",
    "thesis": "2-3 sentence core investment thesis combining all 5 dimensions. The most important cross-dimensional insight.",
    "entry_zone": "$XX–$XX",
    "stop": "$XX",
    "target_6m": "$XX",
    "target_12m": "$XX",
    "best_structure": "stock_long | call_spread | long_call | iron_condor | stock_short | put_spread — and why this structure fits the setup"
  }}
}}"""

    # ── 4. Call Claude Sonnet ────────────────────────────────────────────────
    try:
        msg = _get_client().messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        result = _json.loads(raw)
        result["ticker"] = ticker
        result["price"]  = round(price, 2)
        result["raw_metrics"] = {
            "valuation": val_metrics,
            "hv30": round(hv30, 4),
            "beta": round(beta, 2),
            "pc_ratio": round(pc_ratio, 3),
            "mtf_score": mtf_score,
            "rsi": round(rsi, 1),
            "adx": round(adx, 1),
            "hv_rank": round(hv_rank, 0),
            "earnings_date": earnings_date,
        }
        return result

    except _json.JSONDecodeError:
        return {
            "ticker": ticker,
            "price":  round(price, 2),
            "error":  "JSON parse failed",
            "raw":    locals().get("raw", ""),
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}
