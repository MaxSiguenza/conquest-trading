# -*- coding: utf-8 -*-
"""
Conquest Agent System
=====================
Multi-agent trading intelligence inspired by Ruflo's swarm architecture.
Six specialist agents run in parallel (Claude Haiku), each focused on one
analytical dimension. An Orchestrator (Claude Sonnet) builds weighted consensus.
Agents learn over time — their weights shift based on trade outcomes.

Replaces the random signal-driven trade generator with genuine autonomous
decision-making that improves with every trade closed.

Architecture:
  ┌─────────────────────────────────────────────┐
  │  ConquestAgentSystem.generate_trades()       │
  │                                              │
  │  For each ticker (parallel, 8 workers):      │
  │    ├─ fetch_ticker_data()  — one data pull   │
  │    └─ analyze_ticker()                       │
  │         ├─ 6 specialist agents (parallel)    │
  │         │    each → AgentSignal              │
  │         └─ Orchestrator consensus            │
  │              └─ trade or skip                │
  └─────────────────────────────────────────────┘

  Postgres memory:
    agent_weights      — per-agent accuracy-based vote weight [0.5–2.0]
    agent_trade_log    — which agents voted for which trades
    agent_performance  — running win/loss per agent
"""

import os
import json
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from datetime import datetime, timedelta

import pytz
ET = pytz.timezone("America/New_York")

# ── Optional enrichment keys (activate on Railway by adding env vars) ──────────
FINNHUB_KEY      = os.getenv("FINNHUB_API_KEY", "")
AV_KEY           = os.getenv("ALPHA_VANTAGE_API_KEY", "")

# ── Agent registry ─────────────────────────────────────────────────────────────
AGENT_NAMES = [
    "market_scanner",
    "valuation",
    "technicals",
    "catalysts",
    "risk",
    "options_flow",
    "sentiment",   # Phase 3: StockTwits + social mood
    "news",        # Phase 3: actual headline analysis
]

# Default equal weights — Postgres overrides these once learning starts
DEFAULT_WEIGHTS = {name: 1.0 for name in AGENT_NAMES}

# Consensus thresholds (updated for 8 agents)
MIN_AGENTS_AGREEING   = 5    # at least 5/8 must agree on direction (62.5%)
MIN_WEIGHTED_CONF     = 0.62  # weighted avg confidence must exceed this
RISK_VETO_THRESHOLD   = 0.30  # risk agent confidence below this blocks trade
WEIGHT_LEARN_RATE     = 0.08  # how fast weights shift on win/loss
WEIGHT_MIN, WEIGHT_MAX = 0.40, 2.20


# ── Data containers ─────────────────────────────────────────────────────────────

@dataclass
class TickerData:
    """All data fetched once per ticker, shared across all agents."""
    ticker:        str
    price:         float
    scan:          dict   = field(default_factory=dict)
    info:          dict   = field(default_factory=dict)
    hv30:          float  = 0.0
    hv60:          float  = 0.0
    beta:          float  = 0.0
    max_dd:        float  = 0.0
    pc_ratio:      float  = 0.0
    iv_avg:        float  = 0.0
    call_oi:       float  = 0.0
    put_oi:        float  = 0.0
    top_calls_str: str    = "N/A"
    top_puts_str:  str    = "N/A"
    earnings_date: str    = "Unknown"
    analyst_reco:  str    = "NONE"
    target_upside: float  = 0.0
    w52_low:       float  = 0.0
    w52_high:      float  = 0.0
    # Finnhub enrichment (populated when FINNHUB_API_KEY is set)
    news_sentiment:     float = 0.0   # -0.5 (bearish) → +0.5 (bullish)
    news_count_24h:     int   = 0     # articles in last 24 hours
    insider_sentiment:  str   = "N/A" # BULLISH / BEARISH / NEUTRAL
    analyst_upgrades:   int   = 0     # upgrade count last 3 months
    analyst_downgrades: int   = 0     # downgrade count last 3 months
    # Phase 3: social sentiment + news headlines (no API key required)
    social_bull_pct:    float = 0.0   # StockTwits bullish % of tagged msgs (0–1)
    social_bear_pct:    float = 0.0   # StockTwits bearish %
    social_volume:      int   = 0     # total StockTwits messages (proxy for buzz)
    news_headlines:     str   = ""    # top 4 recent headlines as pipe-separated text
    error:         Optional[str] = None


@dataclass
class AgentSignal:
    """Output from a single specialist agent."""
    agent_name:    str
    ticker:        str
    signal:        str   # BUY | SELL | HOLD | WATCH
    confidence:    float # 0.0–1.0
    reasoning:     str
    suggested_type: str  = ""  # trade structure preferred
    raw:           str   = ""


@dataclass
class GlobalContext:
    """
    Shared macro + portfolio context fetched ONCE per generate_trades() call.
    Passed read-only to every agent so they all see a consistent picture.
    Fetched outside the per-ticker loops so we don't make redundant API calls.
    """
    # ── FRED macro ────────────────────────────────────────────────────────────
    macro_line:   str  = ""     # compact one-liner: "GDP +2.8% | CPI 3.2% YoY | Fed 4.33% | ..."
    yield_normal: bool = True   # False = yield curve inverted = recession warning
    # ── Technical macro regime ────────────────────────────────────────────────
    macro_health: int  = 3      # 0–6 bullish macro indicators count
    regime_phase: str  = "MIXED"        # "MID-CYCLE EXPANSION", "LATE-CYCLE", etc.
    regime_desc:  str  = ""             # one-sentence description of current regime
    best_sectors: list = field(default_factory=list)  # ["Energy", "Financials", ...]
    credit_bull:  bool = True   # HYG golden cross = credit healthy; death cross = stress
    # ── Per-ticker macro warnings ─────────────────────────────────────────────
    macro_warnings: dict = field(default_factory=dict)  # {"AAPL": ["10Y yields rising — tech headwind"]}
    # ── Open portfolio ────────────────────────────────────────────────────────
    open_trades:    list = field(default_factory=list)  # raw open trade dicts
    open_tickers:   set  = field(default_factory=set)   # {"NVDA", "AAPL", ...}
    open_by_type:   dict = field(default_factory=dict)  # {"long_call": 2, "stock_long": 3}
    open_by_ticker: dict = field(default_factory=dict)  # {"NVDA": 2, "AAPL": 1}
    # ── Watchlist ─────────────────────────────────────────────────────────────
    watchlist: dict = field(default_factory=dict)  # {ticker: full watchlist entry dict}


def fetch_global_context(universe: list = None) -> GlobalContext:
    """
    Fetch all shared macro + portfolio context once per trading session.
    All network calls are best-effort — failures return sensible defaults.
    Total added latency: ~2-5s (FRED + macro fetcher run in parallel).
    """
    import time
    t0 = time.time()
    gctx = GlobalContext()

    # ── FRED macro data ───────────────────────────────────────────────────────
    try:
        from macro.fred_data import fetch_fred_macro, fred_macro_context
        fred = fetch_fred_macro()
        gctx.macro_line = fred_macro_context(fred)
        curve = fred.get("T10Y2Y", {})
        if not curve.get("error"):
            gctx.yield_normal = float(curve.get("latest", 0.5) or 0.5) >= 0
    except Exception as e:
        print(f"[GlobalCtx] FRED unavailable ({e})")

    # ── Technical macro regime ────────────────────────────────────────────────
    try:
        from macro.fetcher import (fetch_macro_data, macro_health_score,
                                    sector_rotation_phase, stock_macro_warnings)
        from datetime import datetime as _dt, timedelta as _td
        macro_data = fetch_macro_data((_dt.now() - _td(days=400)).strftime("%Y-%m-%d"))
        score, _ = macro_health_score(macro_data)
        gctx.macro_health = int(score)
        phase, desc, sectors = sector_rotation_phase(macro_data)
        gctx.regime_phase = phase
        gctx.regime_desc  = desc
        gctx.best_sectors = list(sectors)
        hyg = macro_data.get("HYG", {})
        gctx.credit_bull  = int(hyg.get("regime", 1)) == 1
        yc = macro_data.get("YIELD_CURVE", {})
        if not yc.get("error"):
            gctx.yield_normal = int(yc.get("regime", 1)) == 1
        if universe:
            gctx.macro_warnings = stock_macro_warnings(list(universe), macro_data) or {}
    except Exception as e:
        print(f"[GlobalCtx] Macro regime unavailable ({e})")

    # ── Open portfolio ────────────────────────────────────────────────────────
    try:
        from paper_trader import load_trades
        open_t = [t for t in load_trades() if t.get("status") == "open"]
        gctx.open_trades   = open_t
        gctx.open_tickers  = {t["ticker"] for t in open_t}
        for t in open_t:
            tt = t.get("trade_type", "?")
            tk = t.get("ticker",     "?")
            gctx.open_by_type[tt]   = gctx.open_by_type.get(tt, 0)   + 1
            gctx.open_by_ticker[tk] = gctx.open_by_ticker.get(tk, 0) + 1
    except Exception as e:
        print(f"[GlobalCtx] Portfolio unavailable ({e})")

    # ── Watchlist ─────────────────────────────────────────────────────────────
    try:
        from watchlist_engine import load_watchlist
        gctx.watchlist = {e["ticker"]: e for e in load_watchlist() if e.get("ticker")}
    except Exception as e:
        print(f"[GlobalCtx] Watchlist unavailable ({e})")

    print(f"[GlobalCtx] Loaded in {time.time()-t0:.1f}s — "
          f"regime={gctx.regime_phase} health={gctx.macro_health}/6 "
          f"yield={'NORMAL' if gctx.yield_normal else 'INVERTED'} "
          f"credit={'BULL' if gctx.credit_bull else 'STRESS'} "
          f"open={len(gctx.open_trades)} positions "
          f"watchlist={len(gctx.watchlist)} tickers")
    return gctx


# ── Data fetcher ───────────────────────────────────────────────────────────────

def fetch_ticker_data(ticker: str, scan: dict = None) -> TickerData:
    """
    Fetch all data for a ticker in one shot.
    scan dict from alerts.scanner.scan_ticker() can be pre-supplied.
    """
    try:
        import yfinance as yf
        import numpy as np

        tk   = yf.Ticker(ticker)
        info = tk.info or {}
        hist = tk.history(period="6mo", interval="1d", auto_adjust=True)

        price = (info.get("currentPrice")
                 or info.get("regularMarketPrice")
                 or (float(hist["Close"].iloc[-1]) if not hist.empty else 0))

        # Volatility
        hv30 = hv60 = 0.0
        max_dd = 0.0
        if len(hist) >= 20:
            rets  = hist["Close"].pct_change().dropna()
            hv30  = float(rets.tail(21).std() * (252**0.5))
            hv60  = float(rets.tail(42).std() * (252**0.5))
            roll_max = hist["Close"].cummax()
            max_dd   = float(((hist["Close"] - roll_max) / roll_max).min())

        beta = float(info.get("beta") or 0)

        # 52-week range
        w52_low  = float(info.get("fiftyTwoWeekLow",  price * 0.7) or price * 0.7)
        w52_high = float(info.get("fiftyTwoWeekHigh", price * 1.3) or price * 1.3)

        # Analyst
        analyst_reco  = (info.get("recommendationKey") or "none").upper()
        target_mean   = float(info.get("targetMeanPrice") or 0)
        analyst_count = int(info.get("numberOfAnalystOpinions") or 0)
        target_upside = ((target_mean - price) / price * 100) if target_mean and price else 0

        # Earnings
        earnings_date = "Unknown"
        try:
            cal = tk.calendar
            if cal is not None:
                ed = cal.get("Earnings Date") if hasattr(cal, "get") else None
                if ed and len(ed) > 0:
                    earnings_date = str(ed[0])[:10]
        except Exception:
            pass

        # Options flow
        pc_ratio = call_oi = put_oi = iv_avg = 0.0
        top_calls_str = top_puts_str = "N/A"
        try:
            exps = tk.options
            if exps:
                chain   = tk.option_chain(exps[0])
                calls   = chain.calls
                puts    = chain.puts
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
                    f"${r['strike']:.0f} ({int(r['openInterest']):,} OI)"
                    for r in top3c
                )
                top_puts_str = " | ".join(
                    f"${r['strike']:.0f} ({int(r['openInterest']):,} OI)"
                    for r in top3p
                )
        except Exception:
            pass

        # Scanner signals (may be pre-supplied)
        if scan is None:
            try:
                from alerts.scanner import scan_ticker
                scan = scan_ticker(ticker) or {}
            except Exception:
                scan = {}

        # ── Finnhub enrichment (activates when FINNHUB_API_KEY is set) ─────────
        news_sentiment = 0.0
        news_count_24h = 0
        insider_sentiment = "N/A"
        analyst_upgrades = 0
        analyst_downgrades = 0

        if FINNHUB_KEY:
            try:
                import requests as _req

                # 1. News sentiment score
                try:
                    r = _req.get(
                        f"https://finnhub.io/api/v1/news-sentiment?symbol={ticker}&token={FINNHUB_KEY}",
                        timeout=5
                    )
                    if r.status_code == 200:
                        d = r.json()
                        bull_pct = float(d.get("sentiment", {}).get("bullishPercent", 0.5))
                        news_sentiment = round(bull_pct - 0.5, 3)   # −0.5 → +0.5
                        news_count_24h = int(d.get("buzz", {}).get("articlesInLastWeek", 0))
                except Exception:
                    pass

                # 2. Better earnings date from Finnhub calendar
                try:
                    today  = datetime.now().strftime("%Y-%m-%d")
                    future = (datetime.now() + timedelta(days=120)).strftime("%Y-%m-%d")
                    r = _req.get(
                        f"https://finnhub.io/api/v1/calendar/earnings"
                        f"?from={today}&to={future}&symbol={ticker}&token={FINNHUB_KEY}",
                        timeout=5
                    )
                    if r.status_code == 200:
                        cal = r.json().get("earningsCalendar", [])
                        if cal:
                            earnings_date = cal[0].get("date", earnings_date)
                except Exception:
                    pass

                # 3. Insider sentiment (net buy/sell signal)
                try:
                    from_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
                    today     = datetime.now().strftime("%Y-%m-%d")
                    r = _req.get(
                        f"https://finnhub.io/api/v1/stock/insider-sentiment"
                        f"?symbol={ticker}&from={from_date}&to={today}&token={FINNHUB_KEY}",
                        timeout=5
                    )
                    if r.status_code == 200:
                        data = r.json().get("data", [])
                        if data:
                            net_change = sum(d.get("change", 0) for d in data)
                            insider_sentiment = (
                                "BULLISH" if net_change > 0 else
                                ("BEARISH" if net_change < 0 else "NEUTRAL")
                            )
                except Exception:
                    pass

                # 4. Analyst upgrades/downgrades (last 3 months)
                try:
                    r = _req.get(
                        f"https://finnhub.io/api/v1/stock/recommendation?symbol={ticker}&token={FINNHUB_KEY}",
                        timeout=5
                    )
                    if r.status_code == 200:
                        recs = r.json()
                        if recs:
                            latest = recs[0]  # most recent month
                            analyst_upgrades   = int(latest.get("buy",        0)) + int(latest.get("strongBuy",  0))
                            analyst_downgrades = int(latest.get("sell",       0)) + int(latest.get("strongSell", 0))
                except Exception:
                    pass

            except Exception:
                pass   # Finnhub enrichment is always best-effort

        # ── Social sentiment (StockTwits — no API key needed) ─────────────────
        social_bull_pct = social_bear_pct = 0.0
        social_volume   = 0
        try:
            import requests as _sreq
            r = _sreq.get(
                f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json",
                headers={"User-Agent": "ConquestTrading/1.0"},
                timeout=5,
            )
            if r.status_code == 200:
                msgs = r.json().get("messages", [])
                social_volume = len(msgs)
                bull = sum(
                    1 for m in msgs
                    if (m.get("entities") or {}).get("sentiment", {}).get("basic") == "Bullish"
                )
                bear = sum(
                    1 for m in msgs
                    if (m.get("entities") or {}).get("sentiment", {}).get("basic") == "Bearish"
                )
                tagged = bull + bear
                if tagged > 0:
                    social_bull_pct = round(bull / tagged, 3)
                    social_bear_pct = round(bear / tagged, 3)
        except Exception:
            pass

        # ── News headlines (yfinance fallback; Finnhub preferred if key set) ───
        news_headlines = ""
        try:
            headlines = []
            if FINNHUB_KEY:
                try:
                    import requests as _nreq
                    from_dt = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d")
                    today_s = datetime.now().strftime("%Y-%m-%d")
                    r = _nreq.get(
                        f"https://finnhub.io/api/v1/company-news"
                        f"?symbol={ticker}&from={from_dt}&to={today_s}&token={FINNHUB_KEY}",
                        timeout=5,
                    )
                    if r.status_code == 200:
                        articles = r.json()[:5]
                        headlines = [
                            a.get("headline", "")[:100]
                            for a in articles if a.get("headline")
                        ]
                except Exception:
                    pass
            # Fallback: yfinance .news (always available, no key needed)
            if not headlines:
                yf_news = getattr(tk, "news", None) or []
                headlines = [
                    n.get("title", "")[:100]
                    for n in (yf_news[:5] if isinstance(yf_news, list) else [])
                    if n.get("title")
                ]
            news_headlines = " | ".join(headlines[:4])
        except Exception:
            pass

        return TickerData(
            ticker=ticker, price=price, scan=scan, info=info,
            hv30=hv30, hv60=hv60, beta=beta, max_dd=max_dd,
            pc_ratio=pc_ratio, iv_avg=iv_avg, call_oi=call_oi, put_oi=put_oi,
            top_calls_str=top_calls_str, top_puts_str=top_puts_str,
            earnings_date=earnings_date, analyst_reco=analyst_reco,
            target_upside=target_upside, w52_low=w52_low, w52_high=w52_high,
            news_sentiment=news_sentiment, news_count_24h=news_count_24h,
            insider_sentiment=insider_sentiment,
            analyst_upgrades=analyst_upgrades, analyst_downgrades=analyst_downgrades,
            social_bull_pct=social_bull_pct, social_bear_pct=social_bear_pct,
            social_volume=social_volume, news_headlines=news_headlines,
        )

    except Exception as e:
        return TickerData(ticker=ticker, price=0, error=str(e))


# ── Context helpers ────────────────────────────────────────────────────────────

def _portfolio_block(ticker: str, gctx: "GlobalContext") -> str:
    """One-line portfolio exposure note for agent prompts."""
    if gctx is None:
        return ""
    lines = []
    n = gctx.open_by_ticker.get(ticker, 0)
    if n:
        lines.append(f"ALREADY OPEN: {n} position(s) on {ticker} in portfolio.")
    total_open = len(gctx.open_trades)
    if total_open:
        by_type = "  ".join(f"{tt}:{cnt}" for tt, cnt in sorted(gctx.open_by_type.items()))
        lines.append(f"Portfolio: {total_open} open positions [{by_type}]")
    return "\n".join(lines)

def _watchlist_block(ticker: str, gctx: "GlobalContext") -> str:
    """Watchlist entry context for agent prompts, or empty string if not watched."""
    if gctx is None:
        return ""
    wl = gctx.watchlist.get(ticker)
    if not wl:
        return "Watchlist: NOT on watchlist — no pre-vetted thesis."
    parts = [f"Watchlist: CONVICTION={wl.get('conviction','?')} | {wl.get('thesis','')}"]
    if wl.get("entry_zone"):
        parts.append(f"Entry zone: {wl['entry_zone']}  Hard stop: {wl.get('hard_stop','?')}")
    if wl.get("waiting_for"):
        parts.append(f"Waiting for: {wl['waiting_for']}")
    if wl.get("risks"):
        parts.append(f"Key risks: {wl['risks']}")
    return "\n".join(parts)

def _macro_block(ticker: str, gctx: "GlobalContext", mode: str = "brief") -> str:
    """
    Macro context block for agent prompts.
    mode='brief' → one line summary
    mode='full'  → full regime + yield curve + credit health
    """
    if gctx is None or not gctx.macro_line:
        return ""
    if mode == "brief":
        phase_line = f"Macro Regime: {gctx.regime_phase} (health {gctx.macro_health}/6)"
        return phase_line
    # Full mode
    lines = []
    if gctx.macro_line:
        lines.append(f"FRED Macro: {gctx.macro_line}")
    lines.append(f"Regime: {gctx.regime_phase} — {gctx.regime_desc}")
    lines.append(f"Yield Curve: {'NORMAL (non-recessionary)' if gctx.yield_normal else 'INVERTED — recession warning'}")
    lines.append(f"Credit Markets (HYG): {'Golden cross — credit healthy' if gctx.credit_bull else 'Death cross — credit STRESS, risk-off signal'}")
    if gctx.best_sectors:
        lines.append(f"Best sectors this cycle: {', '.join(gctx.best_sectors[:4])}")
    warn = gctx.macro_warnings.get(ticker, [])
    if warn:
        lines.append(f"Macro headwinds for {ticker}: {'; '.join(warn)}")
    return "\n".join(lines)


# ── Specialist agent prompts ───────────────────────────────────────────────────

def _run_agent(agent_name: str, td: TickerData, client,
               gctx: "GlobalContext" = None) -> AgentSignal:
    """
    Run a single specialist agent. Each agent gets only the data
    relevant to its dimension — focused, not overwhelmed.
    """
    sc = td.scan

    # ── Pre-build context blocks ──────────────────────────────────────────────
    portfolio_ctx = _portfolio_block(td.ticker, gctx)
    watchlist_ctx = _watchlist_block(td.ticker, gctx)
    macro_brief   = _macro_block(td.ticker, gctx, mode="brief")
    macro_full    = _macro_block(td.ticker, gctx, mode="full")
    signal_stale  = sc.get("signal_stale", False)
    stale_warn    = "\n⚠ SIGNAL STALE: intraday price action contradicts EOD signal — lower conviction." if signal_stale else ""

    prompts = {

        "market_scanner": f"""You are a market signal scanner agent.
Ticker: {td.ticker} @ ${td.price:.2f}
MTF Score: {sc.get('mtf_score',0)}/3 (M:{sc.get('monthly','?')} / W:{sc.get('weekly','?')} / D:{sc.get('daily','?')})
Entry Signal: {sc.get('entry_signal', False)}
Squeeze Fired: {sc.get('sqz_fired', False)} (momentum: {sc.get('sqz_momentum', 0):+.3f})
MACD Cross Up: {sc.get('macd_cross_up', False)}
RSI: {sc.get('rsi', 50):.1f}  ADX: {sc.get('adx', 20):.1f}
HV Rank: {sc.get('hv_rank', 50):.0f}/100
52W Range: ${td.w52_low:.2f}–${td.w52_high:.2f}
{stale_warn}
{watchlist_ctx}
{portfolio_ctx}
{macro_brief}

Based purely on the signal picture — do these signals justify entering a trade?
A BUY means signals are aligned and high quality. HOLD means signals are weak or mixed. SELL means signals are bearish.
If ALREADY OPEN on this ticker, be more skeptical — we don't want to double-up concentration.
IV RANK RULE: Only suggest long_call or long_put when HV Rank < 35 (options are cheap). HV Rank > 65 → suggest iron_condor, bull_put_spread, or bear_call_spread instead (collect expensive premium, don't pay it).
SQUEEZE RULE: long_call/long_put entries are highest-quality when Squeeze Fired=True — this is the leading indicator that fires BEFORE explosive moves.
Return JSON only: {{"signal":"BUY|SELL|HOLD|WATCH","confidence":0.0-1.0,"reasoning":"one sentence","suggested_type":"call_spread|put_spread|long_call|long_put|iron_condor|stock_long|stock_short|bull_put_spread|bear_call_spread|covered_call"}}""",

        "valuation": f"""You are a fundamental valuation agent.
Ticker: {td.ticker} @ ${td.price:.2f}  Sector: {td.info.get('sector','?')}
Forward P/E: {td.info.get('forwardPE')}   Trailing P/E: {td.info.get('trailingPE')}
P/S (TTM): {td.info.get('priceToSalesTrailing12Months')}   P/B: {td.info.get('priceToBook')}
EV/EBITDA: {td.info.get('enterpriseToEbitda')}   PEG: {td.info.get('pegRatio')}
Revenue Growth: {td.info.get('revenueGrowth')}   Earnings Growth: {td.info.get('earningsGrowth')}
Market Cap: ${(td.info.get('marketCap') or 0)/1e9:.1f}B
Analyst target upside: {td.target_upside:+.1f}%  ({td.analyst_reco})
{macro_brief}
Macro context: High interest rates (Fed Funds {gctx.macro_line.split('Fed ')[1].split(' |')[0] if gctx and 'Fed ' in gctx.macro_line else 'unknown'}) raise discount rates, compressing growth stock multiples. Inverted yield curve = recession risk reduces earnings growth expectations.

Is this stock fairly valued for a trade at current price? BUY = undervalued or growth justifies premium given current rates.
AVOID/SELL = overvalued vs growth or rates headwind. HOLD = fairly valued, no edge.
Return JSON only: {{"signal":"BUY|SELL|HOLD|WATCH","confidence":0.0-1.0,"reasoning":"one sentence","suggested_type":"stock_long|call_spread|long_call|iron_condor|stock_short|put_spread|bull_put_spread"}}""",

        "technicals": f"""You are a technical analysis agent.
Ticker: {td.ticker} @ ${td.price:.2f}
RSI(14): {sc.get('rsi', 50):.1f}   ADX: {sc.get('adx', 20):.1f}
Trend: M:{sc.get('monthly','?')} / W:{sc.get('weekly','?')} / D:{sc.get('daily','?')}
Squeeze Momentum: {sc.get('sqz_momentum', 0):+.3f}
HV Rank: {sc.get('hv_rank', 50):.0f}/100
52W position: ${td.price:.2f} vs low ${td.w52_low:.2f} / high ${td.w52_high:.2f}
Max 6M drawdown: {td.max_dd:.1%}
{macro_brief}
Sector rotation: Best sectors this cycle are {', '.join(gctx.best_sectors[:3]) if gctx and gctx.best_sectors else 'unknown'}. Trading with the rotation adds tailwind.

What is the technical setup? BUY = strong trend + healthy momentum + in a favored sector. SELL = downtrend/breakdown.
HOLD = choppy/consolidating. WATCH = setup forming but not confirmed.
IV RANK RULE: Only suggest long_call or long_put when HV Rank < 35. When HV Rank > 65, options are expensive — suggest iron_condor or credit spreads instead.
Return JSON only: {{"signal":"BUY|SELL|HOLD|WATCH","confidence":0.0-1.0,"reasoning":"one sentence","suggested_type":"call_spread|put_spread|long_call|long_put|iron_condor|stock_long|stock_short|bull_put_spread|bear_call_spread"}}""",

        "catalysts": f"""You are a catalyst analysis agent.
Ticker: {td.ticker} @ ${td.price:.2f}  Sector: {td.info.get('sector','?')}
Next Earnings: {td.earnings_date}
Analyst recommendation: {td.analyst_reco} ({td.info.get('numberOfAnalystOpinions',0)} analysts)
Price target upside: {td.target_upside:+.1f}%
Revenue Growth: {td.info.get('revenueGrowth')}   Earnings Growth: {td.info.get('earningsGrowth')}
News Sentiment (7d): {td.news_sentiment:+.3f}  (scale: -0.5 bearish → +0.5 bullish)  Articles: {td.news_count_24h}
Insider Sentiment (90d): {td.insider_sentiment}
Analyst Upgrades/Downgrades (1mo): {td.analyst_upgrades} upgrades / {td.analyst_downgrades} downgrades
Macro backdrop: {gctx.macro_line if gctx else 'unavailable'}
{stale_warn}

Are there positive catalysts supporting a trade? Weight news sentiment and insider activity heavily.
EARNINGS WITHIN 7 DAYS = elevated binary risk — prefer iron_condor or credit spread over directional trades.
BUY = clear catalyst: bullish news ({td.news_sentiment:+.3f} > 0.1), insider buying, analyst upgrades.
SELL = negative catalyst overhang: bearish news, insider selling, downgrades, macro headwinds.
HOLD = neutral/no near-term catalyst.
Return JSON only: {{"signal":"BUY|SELL|HOLD|WATCH","confidence":0.0-1.0,"reasoning":"one sentence","suggested_type":"call_spread|long_call|stock_long|iron_condor|put_spread|stock_short|bull_put_spread|bear_call_spread"}}""",

        "risk": f"""You are a risk assessment agent. Your job is to protect capital.
Ticker: {td.ticker} @ ${td.price:.2f}
HV30: {td.hv30:.1%}   HV60: {td.hv60:.1%}   Beta: {td.beta:.2f}
Max 6M drawdown: {td.max_dd:.1%}
HV Rank: {sc.get('hv_rank', 50):.0f}/100
IV Avg (options): {td.iv_avg:.1%}

{macro_full}

{portfolio_ctx}
{watchlist_ctx}

Is the risk profile acceptable for a trade right now?
VETO (confidence < 0.30) if ANY of:
  - Yield curve INVERTED AND credit stress (HYG death cross) simultaneously — recession regime
  - Beta > 2.0 AND max drawdown worse than -25%
  - Already have 2+ open positions on this ticker
  - Earnings within 7 days AND HV Rank > 65 (earnings binary + expensive IV)
  - Stock is trading BELOW the watchlist hard_stop price
BUY = risk is manageable, favorable setup.
SELL = risk too high for directional trade, suggest defensive structures.
Return JSON only: {{"signal":"BUY|SELL|HOLD|WATCH","confidence":0.0-1.0,"reasoning":"one sentence citing the specific risk factor","suggested_type":"iron_condor|call_spread|put_spread|long_call|stock_long|stock_short|bull_put_spread|bear_call_spread"}}""",

        "options_flow": f"""You are an options flow intelligence agent.
Ticker: {td.ticker} @ ${td.price:.2f}
Put/Call OI Ratio: {td.pc_ratio:.3f}  (calls: {td.call_oi:,.0f} / puts: {td.put_oi:,.0f})
Avg Call IV: {td.iv_avg:.1%}
Top Call OI strikes: {td.top_calls_str}
Top Put OI strikes: {td.top_puts_str}
HV Rank: {sc.get('hv_rank', 50):.0f}/100
Macro: Yield curve {'NORMAL' if gctx and gctx.yield_normal else 'INVERTED — elevated hedging expected'}. Credit markets {'healthy' if gctx and gctx.credit_bull else 'STRESSED — elevated put buying/hedging'}.

What is smart money positioning signaling?
Inverted yield curve + credit stress = institutions hedge more → expect elevated put OI, high P/C ratio is normal (not always bearish).
BUY = call-heavy flow, unusual upside bets, bullish smart-money positioning vs macro backdrop.
SELL = extreme put loading beyond macro hedging norms — genuine directional bearish bet.
HOLD = balanced flow consistent with macro regime, no clear directional signal.
Return JSON only: {{"signal":"BUY|SELL|HOLD|WATCH","confidence":0.0-1.0,"reasoning":"one sentence","suggested_type":"call_spread|long_call|iron_condor|put_spread|stock_long|stock_short|bull_put_spread|bear_call_spread"}}""",

        "sentiment": f"""You are a social sentiment intelligence agent.
Ticker: {td.ticker} @ ${td.price:.2f}
StockTwits Social: {td.social_bull_pct:.0%} bullish / {td.social_bear_pct:.0%} bearish ({td.social_volume} total messages)
Finnhub Sentiment Score: {td.news_sentiment:+.3f}  (scale: -0.5 bearish → +0.5 bullish, {td.news_count_24h} articles)
Insider Activity (90d): {td.insider_sentiment}
Analyst Upgrades/Downgrades (1mo): {td.analyst_upgrades}↑ / {td.analyst_downgrades}↓
{macro_brief}
{watchlist_ctx}

Social mood is a leading indicator — retail often moves before institutional. But crowded bullish sentiment (>75% bull on StockTwits) can be a contrarian warning sign.
BUY = social sentiment clearly bullish ({td.social_bull_pct:.0%} > 60%) AND Finnhub score positive AND insiders buying. Crowd is aligned, not crowded.
SELL = social clearly bearish OR extreme bullish crowding (contrarian) AND negative Finnhub score.
HOLD = mixed/neutral signals, no strong crowd directional edge.
Zero StockTwits volume = thin coverage — lower your confidence accordingly.
Return JSON only: {{"signal":"BUY|SELL|HOLD|WATCH","confidence":0.0-1.0,"reasoning":"one sentence citing specific sentiment numbers","suggested_type":"call_spread|long_call|put_spread|long_put|stock_long|stock_short|iron_condor|bull_put_spread"}}""",

        "news": f"""You are a news catalyst agent. Your job is to analyze actual recent headlines.
Ticker: {td.ticker} @ ${td.price:.2f}  Sector: {td.info.get('sector','?')}
Recent Headlines: {td.news_headlines if td.news_headlines else 'No headlines available'}
Next Earnings: {td.earnings_date}
Analyst Recommendation: {td.analyst_reco} — Price target upside: {td.target_upside:+.1f}%
Revenue Growth: {td.info.get('revenueGrowth')}   Earnings Growth: {td.info.get('earningsGrowth')}
{macro_full}
{stale_warn}

Read the headlines carefully. Assess whether the news tone is a tailwind or headwind for the stock over the next 30 days.
EARNINGS RULE: If earnings are within 7 days, the headline risk is BINARY — avoid directional trades, suggest iron_condor instead.
BUY = headlines signal product launches, earnings beats, analyst upgrades, partnerships, or macro tailwinds for this sector.
SELL = headlines signal earnings misses, guidance cuts, regulatory risk, leadership issues, or competitive threats.
HOLD = no material news, neutral/routine coverage, or news that's already priced in.
If no headlines are available, output HOLD at low confidence (0.40) — absence of data is not a signal.
Return JSON only: {{"signal":"BUY|SELL|HOLD|WATCH","confidence":0.0-1.0,"reasoning":"one sentence referencing a specific headline or event","suggested_type":"call_spread|long_call|iron_condor|put_spread|stock_long|stock_short|bull_put_spread|bear_call_spread"}}""",
    }

    try:
        from conquest_brain import _get_client
        msg = _get_client().messages.create(
            model="claude-haiku-4-5",
            max_tokens=150,
            messages=[{"role": "user", "content": prompts[agent_name]}],
        )
        raw  = msg.content[0].text.strip()
        clean = raw
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.lower().startswith("json"):
                clean = clean[4:]
            clean = clean.strip()

        parsed = json.loads(clean)
        return AgentSignal(
            agent_name    = agent_name,
            ticker        = td.ticker,
            signal        = parsed.get("signal", "HOLD").upper(),
            confidence    = float(parsed.get("confidence", 0.5)),
            reasoning     = parsed.get("reasoning", ""),
            suggested_type= parsed.get("suggested_type", ""),
            raw           = raw,
        )
    except Exception as e:
        return AgentSignal(
            agent_name=agent_name, ticker=td.ticker,
            signal="HOLD", confidence=0.3,
            reasoning=f"Agent error: {e}", raw="",
        )


# ── Consensus engine ───────────────────────────────────────────────────────────

def build_consensus(signals: list, weights: dict) -> dict:
    """
    Ruflo-inspired consensus mechanism:
    - Confidence-weighted vote per direction
    - Risk agent has hard veto power
    - Need MIN_AGENTS_AGREEING agents on same side
    - Need weighted confidence above MIN_WEIGHTED_CONF

    Returns dict with final decision + vote breakdown.
    """
    if not signals:
        return {"signal": "HOLD", "confidence": 0.0, "should_trade": False,
                "reason": "no signals", "votes": {}, "suggested_type": "iron_condor"}

    # Risk agent veto check
    risk_sig = next((s for s in signals if s.agent_name == "risk"), None)
    if risk_sig and risk_sig.confidence < RISK_VETO_THRESHOLD:
        return {
            "signal": "HOLD", "confidence": 0.0, "should_trade": False,
            "reason": f"Risk agent veto (conf={risk_sig.confidence:.2f}): {risk_sig.reasoning}",
            "votes": {s.agent_name: s.signal for s in signals},
            "suggested_type": "iron_condor",
        }

    # Separate BUY vs SELL votes
    buy_signals  = [s for s in signals if s.signal in ("BUY",)]
    sell_signals = [s for s in signals if s.signal in ("SELL",)]

    def _weighted_conf(sigs):
        total_w = sum(weights.get(s.agent_name, 1.0) for s in sigs)
        if total_w == 0:
            return 0.0
        return sum(s.confidence * weights.get(s.agent_name, 1.0) for s in sigs) / total_w

    buy_wconf  = _weighted_conf(buy_signals)
    sell_wconf = _weighted_conf(sell_signals)

    # Determine dominant direction
    if len(buy_signals) >= MIN_AGENTS_AGREEING and buy_wconf >= MIN_WEIGHTED_CONF:
        direction = "BUY"
        wconf     = buy_wconf
        agreeing  = buy_signals
    elif len(sell_signals) >= MIN_AGENTS_AGREEING and sell_wconf >= MIN_WEIGHTED_CONF:
        direction = "SELL"
        wconf     = sell_wconf
        agreeing  = sell_signals
    else:
        direction = "HOLD"
        wconf     = 0.0
        agreeing  = []

    should_trade = direction in ("BUY", "SELL")

    # Pick best suggested trade structure (most common among agreeing agents)
    type_votes: dict = {}
    for s in agreeing:
        if s.suggested_type:
            type_votes[s.suggested_type] = type_votes.get(s.suggested_type, 0) + \
                                           weights.get(s.agent_name, 1.0)
    suggested_type = (max(type_votes, key=type_votes.get)
                      if type_votes else
                      ("call_spread" if direction == "BUY" else "put_spread"))

    # Override: if HV rank high → prefer iron_condor or credit spreads
    avg_hvr = sum(s.confidence for s in signals) / len(signals)  # proxy

    return {
        "signal":         direction,
        "confidence":     round(wconf, 3),
        "should_trade":   should_trade,
        "suggested_type": suggested_type,
        "agreeing_count": len(agreeing),
        "votes":          {s.agent_name: f"{s.signal}({s.confidence:.2f})" for s in signals},
        "reasoning":      " | ".join(
            f"{s.agent_name}: {s.reasoning}" for s in signals if s.reasoning
        )[:500],
        "reason":         "" if should_trade else
                          f"Consensus not reached ({len(buy_signals)} BUY, {len(sell_signals)} SELL)",
    }


# ── Bull / Bear Debate Round ──────────────────────────────────────────────────
# Inspired by TradingAgents (github.com/TauricResearch/TradingAgents) +
# LLM-TradeBot adversarial framing (github.com/EthanAlgoX/LLM-TradeBot).
#
# Runs ONLY after initial 6-agent consensus says should_trade=True.
# Three steps — each a separate Haiku call:
#   1. Bull agent  — make the strongest specific case FOR the trade
#   2. Bear agent  — adversarially try to KILL the trade (find flaws, not balance)
#   3. Portfolio Manager — weighs both sides and casts the deciding vote
#
# Net effect: catches trades where 4 agents voted BUY on superficial alignment
# rather than genuine thesis strength.  PM can override consensus → SKIP.

def _parse_debate_json(raw: str) -> dict:
    """Strip markdown fences and parse JSON from a debate agent response."""
    clean = raw.strip()
    if clean.startswith("```"):
        parts = clean.split("```")
        clean = parts[1] if len(parts) > 1 else clean
        if clean.lower().startswith("json"):
            clean = clean[4:]
        clean = clean.strip()
    try:
        return json.loads(clean)
    except Exception:
        return {}


def _run_debate_round(td: "TickerData", consensus: dict, signals: list, client,
                      gctx: "GlobalContext" = None) -> dict:
    """
    Bull vs Bear debate.  Returns an enriched consensus dict.
    All three calls are sequential (each depends on the previous).
    Total latency: ~6–10 s (three fast Haiku calls).

    Returns the original consensus unchanged if anything errors out,
    so the debate is always best-effort / non-blocking.
    """
    # Compact context used by all three agents
    signal_summary = "  |  ".join(
        f"{s.agent_name}:{s.signal}({s.confidence:.0%})" for s in signals
    )
    sc = td.scan
    # Watchlist / portfolio lines for the PM
    wl = (gctx.watchlist or {}).get(td.ticker, {}) if gctx else {}
    wl_line = (f"Watchlist: {wl.get('conviction','?')} conviction | {wl.get('thesis','')[:80]}"
               if wl else "Not on watchlist.")
    portfolio_line = (f"Portfolio: {len(gctx.open_trades)} open trades, "
                      f"{gctx.open_by_ticker.get(td.ticker, 0)} already on {td.ticker}"
                      if gctx else "Portfolio: unknown.")
    macro_ctx_line = _macro_block(td.ticker, gctx, mode="full") if gctx else ""

    data_ctx = (
        f"Ticker: {td.ticker} @ ${td.price:.2f}\n"
        f"MTF: {sc.get('mtf_score',0)}/3  RSI: {sc.get('rsi',50):.0f}  "
        f"ADX: {sc.get('adx',20):.0f}  HV Rank: {sc.get('hv_rank',50):.0f}\n"
        f"Beta: {td.beta:.1f}  Max drawdown (6M): {td.max_dd:.1%}  "
        f"Next earnings: {td.earnings_date}\n"
        f"Analyst target upside: {td.target_upside:+.1f}%  Reco: {td.analyst_reco}\n"
        f"News sentiment: {td.news_sentiment:+.3f}  Insider: {td.insider_sentiment}  "
        f"Upgrades/downgrades: {td.analyst_upgrades}↑ / {td.analyst_downgrades}↓\n"
        f"{macro_ctx_line}\n"
        f"{wl_line}\n"
        f"{portfolio_line}\n"
        f"Agent votes: {signal_summary}"
    )

    bull_case     = "Signals are aligned across multiple timeframes."
    bull_strength = 0.60
    bear_case     = "Elevated risk and uncertain macro."
    bear_weakness = 0.40
    pm_decision   = "PROCEED"
    pm_conviction = consensus.get("confidence", 0.65)
    pm_reasoning  = "Debate complete — original consensus maintained."

    try:
        # ── Step 1: Bull advocate ─────────────────────────────────────────────
        r1 = client.messages.create(
            model="claude-haiku-4-5", max_tokens=200,
            messages=[{"role": "user", "content":
                f"""You are a bull-case advocate. Make the STRONGEST case for trading {td.ticker} right now.
{data_ctx}

Cite specific numbers from the data above (RSI, ADX, news sentiment, analyst target, etc.).
What are the 2–3 most compelling reasons this trade works? What is the exact catalyst?
Return JSON only: {{"bull_case":"2-3 sentence argument using specific numbers","strength":0.0-1.0}}"""}]
        )
        d1 = _parse_debate_json(r1.content[0].text)
        bull_case     = d1.get("bull_case", bull_case)
        bull_strength = float(d1.get("strength", bull_strength))
    except Exception:
        pass

    try:
        # ── Step 2: Adversarial bear ──────────────────────────────────────────
        r2 = client.messages.create(
            model="claude-haiku-4-5", max_tokens=200,
            messages=[{"role": "user", "content":
                f"""You are an adversarial risk analyst. Your ONLY job is to PUNCH HOLES in this trade thesis.
{data_ctx}

The bull advocate just argued: "{bull_case}"

Find 2–3 specific weaknesses, data contradictions, or risks they ignored.
You are NOT trying to be balanced — you are trying to kill this trade if it deserves to die.
Cite specific numbers that undercut the bull case.
Return JSON only: {{"bear_case":"2-3 sentence critique with specific data","weakness_score":0.0-1.0}}"""}]
        )
        d2 = _parse_debate_json(r2.content[0].text)
        bear_case     = d2.get("bear_case", bear_case)
        bear_weakness = float(d2.get("weakness_score", bear_weakness))
    except Exception:
        pass

    try:
        # ── Step 3: Portfolio Manager final verdict ───────────────────────────
        r3 = client.messages.create(
            model="claude-haiku-4-5", max_tokens=180,
            messages=[{"role": "user", "content":
                f"""You are the Portfolio Manager. Make the FINAL trade decision on {td.ticker}.

BULL CASE (strength {bull_strength:.0%}): {bull_case}
BEAR CASE (weakness {bear_weakness:.0%}): {bear_case}
Initial consensus: {consensus.get('agreeing_count',0)}/6 agents BUY, confidence {consensus.get('confidence',0):.0%}
{wl_line}
{portfolio_line}
Macro: {'YIELD CURVE INVERTED + CREDIT STRESS — recession regime, raise bar to PROCEED' if gctx and not gctx.yield_normal and not gctx.credit_bull else 'Macro regime: ' + (gctx.regime_phase if gctx else 'unknown')}

Decision rules (apply strictly):
- SKIP if yield curve INVERTED AND credit markets STRESSED (HYG death cross) — recession regime, only defensive structures
- SKIP if already have 2+ open positions on this ticker — avoid concentration
- SKIP if bear_weakness > 0.70 AND bull strength < 0.60  (weak bull + valid bear objections)
- SKIP if earnings within 7 days and high IV (earnings binary — use iron_condor instead)
- SKIP if beta > 2.0 and max_dd worse than -25% (portfolio risk too high)
- PROCEED if bull strength ≥ 0.65 AND bear weakness ≤ 0.55
- Default: lean toward caution — missing a trade costs nothing, a bad trade costs capital

Return JSON only: {{"decision":"PROCEED|SKIP","conviction":0.0-1.0,"reasoning":"one sentence"}}"""}]
        )
        d3 = _parse_debate_json(r3.content[0].text)
        pm_decision   = d3.get("decision", "PROCEED").upper()
        pm_conviction = float(d3.get("conviction", pm_conviction))
        pm_reasoning  = d3.get("reasoning", pm_reasoning)
    except Exception:
        pass

    # If PM vetoed — log to missed-trades cache for Discord to read
    if pm_decision == "SKIP":
        global _vetoed_trades
        _vetoed_trades.append({
            "ticker":        td.ticker,
            "price":         td.price,
            "sector":        td.info.get("sector", "?"),
            "initial_conf":  consensus.get("confidence", 0),
            "agent_count":   consensus.get("agreeing_count", 0),
            "bull_case":     bull_case,
            "bull_strength": bull_strength,
            "bear_case":     bear_case,
            "bear_weakness": bear_weakness,
            "pm_reasoning":  pm_reasoning,
            "suggested_type":consensus.get("suggested_type", ""),
        })

    return {
        **consensus,
        "should_trade":   pm_decision == "PROCEED",
        "confidence":     pm_conviction,
        "bull_case":      bull_case,
        "bull_strength":  bull_strength,
        "bear_case":      bear_case,
        "bear_weakness":  bear_weakness,
        "pm_decision":    pm_decision,
        "pm_reasoning":   pm_reasoning,
        "debate_ran":     True,
    }


# ── Main system class ──────────────────────────────────────────────────────────

class ConquestAgentSystem:
    """
    The fully autonomous multi-agent trading brain.
    Drop-in replacement for generate_daily_trades() in paper_trader.py.
    """

    def __init__(self):
        self.weights = self._load_weights()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load_weights(self) -> dict:
        try:
            from db import kv_get
            saved = kv_get("agent_weights")
            if isinstance(saved, dict):
                # Merge with defaults so new agents always have a weight
                return {**DEFAULT_WEIGHTS, **saved}
        except Exception:
            pass
        return DEFAULT_WEIGHTS.copy()

    def _save_weights(self):
        try:
            from db import kv_set
            kv_set("agent_weights", self.weights)
        except Exception:
            pass

    def _log_trade_votes(self, trade_id: str, signals: list, consensus: dict):
        """Store which agents voted for this trade for later learning."""
        try:
            from db import kv_get, kv_set
            log = kv_get("agent_trade_log") or {}
            log[trade_id] = {
                "votes":     {s.agent_name: {"signal": s.signal, "conf": s.confidence}
                              for s in signals},
                "consensus": consensus,
                "ts":        datetime.now(ET).isoformat(),
            }
            # Keep last 200 entries only
            if len(log) > 200:
                oldest = sorted(log.keys())[:-200]
                for k in oldest:
                    del log[k]
            kv_set("agent_trade_log", log)
        except Exception:
            pass

    # ── Core analysis pipeline ────────────────────────────────────────────────

    def analyze_ticker(self, ticker: str, scan: dict = None,
                       gctx: "GlobalContext" = None) -> dict:
        """
        Full agent swarm analysis for one ticker.
        Runs all 6 specialist agents in parallel, then builds consensus.
        gctx is shared global context (macro + portfolio) fetched once per session.
        """
        td = fetch_ticker_data(ticker, scan=scan)
        if td.error or not td.price:
            return {"ticker": ticker, "should_trade": False,
                    "reason": td.error or "no price", "signals": [], "consensus": {}}

        # Run all agents in parallel — pass gctx to every agent
        signals = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            from conquest_brain import _get_client
            client = _get_client()
            futs = {
                pool.submit(_run_agent, name, td, client, gctx): name
                for name in AGENT_NAMES
            }
            for fut in as_completed(futs):
                try:
                    signals.append(fut.result())
                except Exception as e:
                    agent = futs[fut]
                    signals.append(AgentSignal(
                        agent_name=agent, ticker=ticker,
                        signal="HOLD", confidence=0.3,
                        reasoning=f"failed: {e}",
                    ))

        consensus = build_consensus(signals, self.weights)

        # ── Debate round: only if initial consensus says trade ────────────────
        # Bull advocate → adversarial bear → Portfolio Manager final verdict.
        # ~6-10 extra seconds but catches weak-thesis false positives.
        if consensus.get("should_trade"):
            try:
                from conquest_brain import _get_client as _gc
                consensus = _run_debate_round(td, consensus, signals, _gc(), gctx=gctx)
                verdict = consensus.get("pm_decision", "?")
                print(
                    f"[Debate] {ticker}: PM says {verdict} "
                    f"(bull {consensus.get('bull_strength',0):.0%} / "
                    f"bear {consensus.get('bear_weakness',0):.0%}) — "
                    f"{consensus.get('pm_reasoning','')}"
                )
            except Exception as _de:
                print(f"[Debate] {ticker}: debate errored ({_de}) — keeping original consensus")

        return {
            "ticker":       ticker,
            "price":        td.price,
            "td":           td,
            "signals":      signals,
            "consensus":    consensus,
            "should_trade": consensus["should_trade"],
        }

    # ── Trade generation ──────────────────────────────────────────────────────

    # ── Correlation helpers ───────────────────────────────────────────────────

    @staticmethod
    def _price_returns(ticker: str, days: int = 30) -> list:
        """30-day daily returns for pairwise correlation checks."""
        try:
            import yfinance as yf
            hist = yf.download(ticker, period=f"{days}d", interval="1d",
                               auto_adjust=True, progress=False)
            if len(hist) < 10:
                return []
            return list(hist["Close"].pct_change().dropna())
        except Exception:
            return []

    @staticmethod
    def _pearson(a: list, b: list) -> float:
        """Pearson correlation between two return series."""
        try:
            import numpy as np
            n = min(len(a), len(b))
            if n < 10:
                return 0.0
            va = np.array(a[-n:])
            vb = np.array(b[-n:])
            if va.std() < 1e-10 or vb.std() < 1e-10:
                return 0.0
            return float(np.corrcoef(va, vb)[0, 1])
        except Exception:
            return 0.0

    def _too_correlated(self, ticker: str, selected_returns: dict,
                        threshold: float = 0.78) -> bool:
        """
        True if `ticker` has correlation > threshold with ANY already-selected trade.
        Fetches returns lazily (cached in selected_returns dict).
        threshold=0.78 means "very similar price action" — lower = stricter.
        """
        new_ret = self._price_returns(ticker)
        if not new_ret:
            return False   # can't check → allow it
        for sel_ticker, sel_ret in selected_returns.items():
            if not sel_ret:
                continue
            corr = self._pearson(new_ret, sel_ret)
            if corr > threshold:
                print(f"[AgentSystem] ⚠ {ticker} corr={corr:.2f} with {sel_ticker} "
                      f"— skipping (too correlated, protects from sector concentration)")
                return True
        selected_returns[ticker] = new_ret   # cache for future comparisons
        return False

    # ── Trade generation ──────────────────────────────────────────────────────

    def generate_trades(self, universe: list, n: int = 10,
                        existing_tickers: set = None) -> list:
        """
        Autonomous trade generation. Analyzes every ticker in universe,
        selects the highest-conviction consensus trades up to n.

        Includes two diversification guards:
          1. Type cap: no more than 3 trades of the same structure
          2. Correlation filter: blocks trades that move in lockstep
             with already-selected positions (>0.78 Pearson, 30-day returns)

        Returns list of trade dicts in the same format as paper_trader.py
        so the rest of the system works unchanged.
        """
        from paper_trader import _build_trade
        existing_tickers = existing_tickers or set()
        ts = datetime.now(ET).strftime("%Y-%m-%dT%H:%M")

        _clear_vetoed_trades()   # reset cache for this run

        # Fetch macro + portfolio context once; share across all ticker analyses
        gctx = fetch_global_context(universe)
        # Merge portfolio's already-open tickers with caller-supplied exclusions
        all_existing = existing_tickers | gctx.open_tickers

        print(f"[AgentSystem] Analyzing {len(universe)} tickers with 6-agent swarm…")

        # Parallel ticker analysis (8 workers — one per ticker group)
        results = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(self.analyze_ticker, t, None, gctx): t
                    for t in universe if t not in all_existing}
            for fut in as_completed(futs):
                try:
                    results.append(fut.result())
                except Exception as e:
                    print(f"[AgentSystem] Ticker analysis error: {e}")

        # Sort: should_trade first, then by consensus confidence
        tradeable = [r for r in results if r.get("should_trade")]
        tradeable.sort(key=lambda r: r["consensus"].get("confidence", 0), reverse=True)

        print(f"[AgentSystem] {len(tradeable)}/{len(results)} tickers passed consensus.")

        new_trades    = []
        type_counts:  dict = {}
        sel_returns:  dict = {}   # ticker → returns list (correlation cache)

        for r in tradeable:
            if len(new_trades) >= n:
                break

            ticker     = r["ticker"]
            consensus  = r["consensus"]
            td         = r.get("td")
            signals    = r.get("signals", [])
            trade_type = consensus.get("suggested_type", "call_spread")

            # ── Guard 1: cap each trade type at 3 ────────────────────────────
            if type_counts.get(trade_type, 0) >= 3:
                alts = [tt for tt in [
                    "call_spread","put_spread","long_call","long_put",
                    "iron_condor","stock_long","stock_short",
                    "bull_put_spread","covered_call",
                ] if type_counts.get(tt, 0) < 3]
                if not alts:
                    break
                trade_type = min(alts, key=lambda tt: type_counts.get(tt, 0))

            # ── Guard 2: correlation filter — no sector concentration ─────────
            # Skip if this ticker moves too similarly to an already-selected one.
            # ETFs are exempt (SPY/QQQ are useful even when correlated).
            is_etf = ticker in ("SPY", "QQQ", "IWM", "XLF", "XLE", "XLK",
                                 "XLV", "GLD", "TLT", "XLI", "XLY", "XLP")
            if not is_etf and len(new_trades) > 0:
                if self._too_correlated(ticker, sel_returns):
                    continue   # skip — try next best candidate

            # Build scan dict for paper_trader's _build_trade
            scan = r["td"].scan if td else {}
            scan["price"] = r["price"]
            scan["ticker"] = ticker

            trade = _build_trade(scan, trade_type, ts)
            if not trade:
                continue

            # Attach agent metadata + debate results
            trade["agent_consensus"]  = consensus.get("signal")
            trade["agent_confidence"] = consensus.get("confidence", 0)
            trade["agent_votes"]      = consensus.get("votes", {})
            trade["agent_count"]      = consensus.get("agreeing_count", 0)
            trade["adx_entry"]        = round(scan.get("adx", 0), 1)
            # Debate round fields (present only when debate ran)
            if consensus.get("debate_ran"):
                trade["debate_bull"]      = consensus.get("bull_case", "")
                trade["debate_bull_str"]  = round(consensus.get("bull_strength", 0), 2)
                trade["debate_bear"]      = consensus.get("bear_case", "")
                trade["debate_bear_weak"] = round(consensus.get("bear_weakness", 0), 2)
                trade["debate_pm"]        = consensus.get("pm_reasoning", "")

            # ── Conviction-based sizing (stock trades only) ───────────────────
            # Options stay at 1 contract — doubling contracts doubles max loss,
            # too aggressive while the system is still being calibrated.
            # Stocks can scale notional safely: high conviction = 1.5× size.
            conf = consensus.get("confidence", 0.62)
            if trade["trade_type"] in ("stock_long", "stock_short"):
                if conf >= 0.82:
                    mult = 1.50   # very high conviction → $1,500 notional
                elif conf >= 0.72:
                    mult = 1.25   # solid conviction → $1,250 notional
                else:
                    mult = 1.00   # standard → $1,000 notional
                if mult > 1.0:
                    trade["shares"]     = round(trade.get("shares", 1) * mult)
                    trade["cost_basis"] = round(trade.get("cost_basis", 0) * mult, 2)
                    trade["size_mult"]  = mult
            # Tag every trade with conviction tier — visible in Discord + Notion
            trade["conviction_tier"] = (
                "HIGH"   if conf >= 0.82 else
                "MED"    if conf >= 0.72 else
                "STD"
            )

            new_trades.append(trade)
            type_counts[trade_type] = type_counts.get(trade_type, 0) + 1

            # Log votes for learning
            self._log_trade_votes(trade["id"], signals, consensus)

        # If agent system couldn't fill n trades, log it
        if len(new_trades) < n:
            print(f"[AgentSystem] Only found {len(new_trades)} high-conviction trades "
                  f"(needed {n}). Staying disciplined — no low-conviction fills.")

        return new_trades

    # ── Learning: update weights after trade closes ───────────────────────────

    # Trade type → required market direction for the trade to profit
    # This determines which agent signals were "correct" after a close.
    _TRADE_DIRECTION = {
        "stock_long":      "BUY",   # needs price up
        "long_call":       "BUY",
        "call_spread":     "BUY",
        "covered_call":    "BUY",   # profits when stock stays flat/up above cost basis
        "bull_put_spread": "BUY",   # profits when stock stays above short put strike
        "stock_short":     "SELL",  # needs price down
        "long_put":        "SELL",
        "put_spread":      "SELL",
        "bear_call_spread":"SELL",
        # iron_condor profits from STILLNESS — neither BUY nor SELL is correct,
        # so we skip learning on these to avoid corrupting directional weights
    }

    def update_weights_from_trade(self, trade: dict):
        """
        Called when a trade closes. Reward agents that correctly predicted
        the outcome, penalize agents that were wrong.

        Win  → agents whose signal matched the trade direction gain weight
        Loss → those same agents lose weight
        Iron condors are skipped — they profit from low volatility, not direction,
        and mapping them to BUY/SELL would corrupt directional agent weights.
        """
        try:
            from db import kv_get
            log = kv_get("agent_trade_log") or {}
            entry = log.get(trade["id"])
            if not entry:
                return

            trade_direction = self._TRADE_DIRECTION.get(trade["trade_type"])
            if trade_direction is None:
                # iron_condor or unknown type — skip learning for this trade
                return

            won = trade.get("pnl", 0) > 0

            for agent_name, vote_data in entry["votes"].items():
                agent_signal = vote_data["signal"]
                correct = (agent_signal == trade_direction and won) or \
                          (agent_signal != trade_direction and not won)

                delta = WEIGHT_LEARN_RATE if correct else -WEIGHT_LEARN_RATE
                old_w = self.weights.get(agent_name, 1.0)
                new_w = max(WEIGHT_MIN, min(WEIGHT_MAX, old_w + delta))
                self.weights[agent_name] = round(new_w, 4)

            self._save_weights()
            print(f"[AgentSystem] Weights updated from {trade['ticker']} "
                  f"({trade['trade_type']}) {'WIN' if won else 'LOSS'}. "
                  f"Weights: {self.weights}")

        except Exception as e:
            print(f"[AgentSystem] Weight update failed: {e}")

    # ── Status / reporting ────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Returns a snapshot of agent weights + performance for Discord/dashboard."""
        try:
            from db import kv_get
            log  = kv_get("agent_trade_log") or {}
            perf = {name: {"weight": self.weights.get(name, 1.0),
                           "votes": 0} for name in AGENT_NAMES}
            for entry in log.values():
                for name in entry.get("votes", {}):
                    if name in perf:
                        perf[name]["votes"] += 1
            return {"weights": self.weights, "per_agent": perf,
                    "total_logged": len(log)}
        except Exception as e:
            return {"error": str(e), "weights": self.weights}


# ── Missed-trade cache (PM debate vetoes) ─────────────────────────────────────
# Populated when _run_debate_round returns pm_decision == "SKIP".
# Discord bot reads this after trade generation to post to #missed-trades.
_vetoed_trades: list = []   # list of dicts: ticker, price, bull_case, bear_case, pm_reasoning


def get_vetoed_trades() -> list:
    """Return vetoed trades from the most recent generate_trades() run."""
    return list(_vetoed_trades)


def _clear_vetoed_trades():
    global _vetoed_trades
    _vetoed_trades = []


# ── Module-level singleton ─────────────────────────────────────────────────────
_system: Optional[ConquestAgentSystem] = None

def get_agent_system() -> ConquestAgentSystem:
    global _system
    if _system is None:
        _system = ConquestAgentSystem()
    return _system
