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
]

# Default equal weights — Postgres overrides these once learning starts
DEFAULT_WEIGHTS = {name: 1.0 for name in AGENT_NAMES}

# Consensus thresholds (Ruflo-inspired)
MIN_AGENTS_AGREEING   = 4    # at least 4/6 must agree on direction
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
    error:         Optional[str] = None


@dataclass
class AgentSignal:
    """Output from a single specialist agent."""
    agent_name:    str
    ticker:        str
    signal:        str   # BUY | SELL | HOLD | WATCH
    confidence:    float # 0.0–1.0
    reasoning:     str
    suggested_type: str  = ""  # trade structure preference
    raw:           str   = ""


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
        )

    except Exception as e:
        return TickerData(ticker=ticker, price=0, error=str(e))


# ── Specialist agent prompts ───────────────────────────────────────────────────

def _run_agent(agent_name: str, td: TickerData, client) -> AgentSignal:
    """
    Run a single specialist agent. Each agent gets only the data
    relevant to its dimension — focused, not overwhelmed.
    """
    sc = td.scan

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

Based purely on the signal picture — do these signals justify entering a trade?
A BUY means signals are aligned and high quality. HOLD means signals are weak or mixed. SELL means signals are bearish.
Return JSON only: {{"signal":"BUY|SELL|HOLD|WATCH","confidence":0.0-1.0,"reasoning":"one sentence","suggested_type":"call_spread|put_spread|long_call|long_put|iron_condor|stock_long|stock_short"}}""",

        "valuation": f"""You are a fundamental valuation agent.
Ticker: {td.ticker} @ ${td.price:.2f}  Sector: {td.info.get('sector','?')}
Forward P/E: {td.info.get('forwardPE')}   Trailing P/E: {td.info.get('trailingPE')}
P/S (TTM): {td.info.get('priceToSalesTrailing12Months')}   P/B: {td.info.get('priceToBook')}
EV/EBITDA: {td.info.get('enterpriseToEbitda')}   PEG: {td.info.get('pegRatio')}
Revenue Growth: {td.info.get('revenueGrowth')}   Earnings Growth: {td.info.get('earningsGrowth')}
Market Cap: ${(td.info.get('marketCap') or 0)/1e9:.1f}B
Analyst target upside: {td.target_upside:+.1f}%  ({td.analyst_reco})

Is this stock fairly valued for a trade at current price? BUY = undervalued or growth justifies premium.
AVOID/SELL = overvalued vs growth. HOLD = fairly valued, no edge.
Return JSON only: {{"signal":"BUY|SELL|HOLD|WATCH","confidence":0.0-1.0,"reasoning":"one sentence","suggested_type":"stock_long|call_spread|long_call|iron_condor|stock_short|put_spread"}}""",

        "technicals": f"""You are a technical analysis agent.
Ticker: {td.ticker} @ ${td.price:.2f}
RSI(14): {sc.get('rsi', 50):.1f}   ADX: {sc.get('adx', 20):.1f}
Trend: M:{sc.get('monthly','?')} / W:{sc.get('weekly','?')} / D:{sc.get('daily','?')}
Squeeze Momentum: {sc.get('sqz_momentum', 0):+.3f}
HV Rank: {sc.get('hv_rank', 50):.0f}/100
52W position: ${td.price:.2f} vs low ${td.w52_low:.2f} / high ${td.w52_high:.2f}
Max 6M drawdown: {td.max_dd:.1%}

What is the technical setup? BUY = strong trend + healthy momentum. SELL = downtrend/breakdown.
HOLD = choppy/consolidating. WATCH = setup forming but not confirmed.
Return JSON only: {{"signal":"BUY|SELL|HOLD|WATCH","confidence":0.0-1.0,"reasoning":"one sentence","suggested_type":"call_spread|put_spread|long_call|long_put|iron_condor|stock_long|stock_short"}}""",

        "catalysts": f"""You are a catalyst analysis agent.
Ticker: {td.ticker} @ ${td.price:.2f}  Sector: {td.info.get('sector','?')}
Next Earnings: {td.earnings_date}
Analyst recommendation: {td.analyst_reco} ({td.info.get('numberOfAnalystOpinions',0)} analysts)
Price target upside: {td.target_upside:+.1f}%
Revenue Growth: {td.info.get('revenueGrowth')}
Earnings Growth: {td.info.get('earningsGrowth')}
News Sentiment (7d): {td.news_sentiment:+.3f}  (scale: -0.5 bearish → +0.5 bullish)  Articles: {td.news_count_24h}
Insider Sentiment (90d): {td.insider_sentiment}
Analyst Upgrades/Downgrades (1mo): {td.analyst_upgrades} upgrades / {td.analyst_downgrades} downgrades

Are there positive catalysts supporting a trade? Weight news sentiment and insider activity heavily —
positive insider buying and bullish news sentiment are strong near-term catalysts.
BUY = clear catalyst: bullish news ({td.news_sentiment:+.3f} > 0.1), insider buying, upcoming earnings + analyst upgrades.
SELL = negative catalyst overhang: bearish news, insider selling, analyst downgrades.
HOLD = neutral/no near-term catalyst.
Return JSON only: {{"signal":"BUY|SELL|HOLD|WATCH","confidence":0.0-1.0,"reasoning":"one sentence","suggested_type":"call_spread|long_call|stock_long|iron_condor|put_spread|stock_short"}}""",

        "risk": f"""You are a risk assessment agent. Your job is to protect capital.
Ticker: {td.ticker} @ ${td.price:.2f}
HV30: {td.hv30:.1%}   HV60: {td.hv60:.1%}   Beta: {td.beta:.2f}
Max 6M drawdown: {td.max_dd:.1%}
HV Rank: {sc.get('hv_rank', 50):.0f}/100
IV Avg (options): {td.iv_avg:.1%}

Is the risk profile acceptable for a trade right now?
BUY = low/medium risk, favorable risk-reward setup.
HOLD = risk is elevated but manageable.
SELL = risk is too high, position sizing dangerous.
If confidence is below 0.30, you are effectively vetoing the trade.
Return JSON only: {{"signal":"BUY|SELL|HOLD|WATCH","confidence":0.0-1.0,"reasoning":"one sentence with specific risk concern","suggested_type":"iron_condor|call_spread|put_spread|long_call|stock_long|stock_short"}}""",

        "options_flow": f"""You are an options flow intelligence agent.
Ticker: {td.ticker} @ ${td.price:.2f}
Put/Call OI Ratio: {td.pc_ratio:.3f}  (calls: {td.call_oi:,.0f} / puts: {td.put_oi:,.0f})
Avg Call IV: {td.iv_avg:.1%}
Top Call OI strikes: {td.top_calls_str}
Top Put OI strikes: {td.top_puts_str}
HV Rank: {sc.get('hv_rank', 50):.0f}/100

What is smart money positioning signaling?
BUY = call-heavy flow, unusual upside bets, bullish positioning.
SELL = put-heavy flow, hedging, bearish institutional bets.
HOLD = balanced flow, no clear signal.
Return JSON only: {{"signal":"BUY|SELL|HOLD|WATCH","confidence":0.0-1.0,"reasoning":"one sentence","suggested_type":"call_spread|long_call|iron_condor|put_spread|stock_long|stock_short"}}""",
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

    def analyze_ticker(self, ticker: str, scan: dict = None) -> dict:
        """
        Full agent swarm analysis for one ticker.
        Runs all 6 specialist agents in parallel, then builds consensus.
        """
        td = fetch_ticker_data(ticker, scan=scan)
        if td.error or not td.price:
            return {"ticker": ticker, "should_trade": False,
                    "reason": td.error or "no price", "signals": [], "consensus": {}}

        # Run all agents in parallel
        signals = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            from conquest_brain import _get_client
            client = _get_client()
            futs = {
                pool.submit(_run_agent, name, td, client): name
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

        print(f"[AgentSystem] Analyzing {len(universe)} tickers with 6-agent swarm…")

        # Parallel ticker analysis (8 workers — one per ticker group)
        results = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(self.analyze_ticker, t): t
                    for t in universe if t not in existing_tickers}
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
                    "iron_condor","stock_long","stock_short"
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

            # Attach agent metadata
            trade["agent_consensus"]  = consensus.get("signal")
            trade["agent_confidence"] = consensus.get("confidence", 0)
            trade["agent_votes"]      = consensus.get("votes", {})
            trade["agent_count"]      = consensus.get("agreeing_count", 0)
            trade["adx_entry"]        = round(scan.get("adx", 0), 1)

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

    def update_weights_from_trade(self, trade: dict):
        """
        Called when a trade closes. Reward agents that correctly predicted
        the outcome, penalize agents that were wrong.
        Win  → agents who said BUY (for a BUY trade that won) gain weight
        Loss → those same agents lose weight
        """
        try:
            from db import kv_get
            log = kv_get("agent_trade_log") or {}
            entry = log.get(trade["id"])
            if not entry:
                return

            trade_direction = "BUY" if trade["trade_type"] in (
                "stock_long","call_spread","long_call") else "SELL"
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
                  f"{'WIN' if won else 'LOSS'}. Weights: {self.weights}")

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


# ── Module-level singleton ─────────────────────────────────────────────────────
_system: Optional[ConquestAgentSystem] = None

def get_agent_system() -> ConquestAgentSystem:
    global _system
    if _system is None:
        _system = ConquestAgentSystem()
    return _system
