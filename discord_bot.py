# -*- coding: utf-8 -*-
"""
Conquest Trading — Discord Bot
================================
A full Discord bot. No more webhook URLs — one bot token handles everything.

COMMANDS (type these anywhere in your Discord server):
  !scan              — scan your full watchlist for signals
  !scan AAPL NVDA    — scan specific tickers
  !analyze AAPL      — deep dive on one ticker with Claude commentary
  !portfolio         — live P&L on all paper positions
  !briefing          — morning briefing with FRED macro + Claude analysis
  !macro             — quick Fed macro snapshot
  !pnl               — send daily P&L to positions channel
  !help              — show all commands

AUTO TASKS:
  Morning briefing fires at 9:00 AM ET Mon–Fri when auto_briefing=true
  (Check the box on the Alerts page, no webhook URL needed)

SETUP (one-time):
  1.  discord.com/developers/applications → New Application → e.g. "Conquest Bot"
  2.  Bot → Add Bot → Reset Token → copy it
  3.  Under "Privileged Gateway Intents" enable: Message Content Intent
  4.  OAuth2 → URL Generator → select "bot" → check:
        Send Messages, Embed Links, Read Message History, Read Messages/View Channels
  5.  Copy the generated URL → paste in browser → add bot to your server
  6.  Add to .env:   DISCORD_BOT_TOKEN=your-token-here
  7.  python discord_bot.py
"""

import discord
from discord.ext import commands, tasks
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

APP_DIR      = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(APP_DIR, "alerts_settings.json")
ENV_FILE      = os.path.join(APP_DIR, ".env")
sys.path.insert(0, APP_DIR)

# Discord embed colours
COLOR_GREEN  = 0x4ade80
COLOR_RED    = 0xf87171
COLOR_PURPLE = 0x7c6af7
COLOR_ORANGE = 0xfb923c
COLOR_GOLD   = 0xfbbf24
COLOR_DARK   = 0x2d3148


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _get_token() -> str:
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(ENV_FILE)
        return vals.get("DISCORD_BOT_TOKEN", "") or os.getenv("DISCORD_BOT_TOKEN", "")
    except Exception:
        return os.getenv("DISCORD_BOT_TOKEN", "")


async def _run_sync(func, *args):
    """Run a blocking sync function in a thread without freezing the bot."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args)


def _ts() -> datetime:
    return datetime.now(timezone.utc)


# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True  # required to read message content

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,   # we write our own
)


# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"\n⚔️  Conquest Bot online  →  {bot.user}")
    print(f"   Servers:  {len(bot.guilds)}")
    print(f"   Commands: !scan  !analyze  !portfolio  !briefing  !macro  !pnl  !stats  !trades  !help")
    print(f"   Auto tasks starting...\n")
    if not morning_briefing_task.is_running():
        morning_briefing_task.start()
    if not paper_trading_loop.is_running():
        paper_trading_loop.start()


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return   # silently ignore unknown commands
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠ Missing argument. Try `!help` for usage.", delete_after=12)
    else:
        await ctx.send(f"⚠ {str(error)[:200]}", delete_after=15)


# ── !help ─────────────────────────────────────────────────────────────────────

@bot.command(name="help", aliases=["h", "commands"])
async def help_cmd(ctx):
    embed = discord.Embed(
        title="⚔️  Conquest Trading Bot",
        description=(
            "Your trading intelligence, available directly in Discord.\n"
            "Works from your phone — no need to open the web app."
        ),
        color=COLOR_PURPLE,
        timestamp=_ts(),
    )
    embed.add_field(name="📡  Signal Scanning", value=(
        "`!scan` — scan your full watchlist\n"
        "`!scan AAPL NVDA MSFT` — scan specific tickers\n"
        "`!analyze AAPL` — deep dive + Claude analyst note"
    ), inline=False)
    embed.add_field(name="📋  Paper Portfolio", value=(
        "`!portfolio` — live P&L on all positions\n"
        "`!pnl` — send daily P&L to positions channel"
    ), inline=False)
    embed.add_field(name="🧪  Paper Trading Stats", value=(
        "`!stats` — win rate, P&L, Sharpe, by-type breakdown\n"
        "`!trades` — today's 10 auto-generated paper trades\n"
        "`!generate` — manually trigger today's 10 trades"
    ), inline=False)
    embed.add_field(name="⚔️  Intelligence", value=(
        "`!briefing` — morning briefing with FRED macro data\n"
        "`!macro` — quick Fed macro snapshot"
    ), inline=False)
    embed.add_field(name="⚙️  Settings & Diagnostics", value=(
        "Go to the **Alerts** page to set your watchlist and\n"
        "toggle the 9 AM auto-briefing on/off.\n"
        "`!testchannels` — fire a test message to every channel to verify routing"
    ), inline=False)
    embed.set_footer(text="Conquest Trading  •  Not financial advice  •  Always DYOR")
    await ctx.send(embed=embed)


# ── !scan ─────────────────────────────────────────────────────────────────────

@bot.command(name="scan", aliases=["s"])
async def scan_cmd(ctx, *tickers):
    s         = _load_settings()
    watchlist = list(tickers) if tickers else s.get("watchlist", "").split()

    if not watchlist:
        await ctx.send(
            "No watchlist configured. Either:\n"
            "• `!scan AAPL NVDA MSFT` to scan specific tickers\n"
            "• Set a watchlist on the Alerts page at http://localhost:5000/alerts"
        )
        return

    thinking = await ctx.send(f"⏳ Scanning **{len(watchlist)}** ticker(s) in parallel...")

    def _do_scan():
        from alerts.scanner import scan_watchlist
        return scan_watchlist(watchlist)

    results = await _run_sync(_do_scan)
    entries = [r for r in results if r.get("entry_signal")    and not r.get("error")]
    crosses = [r for r in results if r.get("macd_cross_up")   and not r.get("entry_signal") and not r.get("error")]
    errors  = [r for r in results if r.get("error")]

    if entries:
        color = COLOR_GREEN
        title = f"✦  {len(entries)} Entry Signal(s) Found"
    elif crosses:
        color = COLOR_ORANGE
        title = f"↑  {len(crosses)} MACD Cross(es) — Momentum Shifting"
    else:
        color = COLOR_DARK
        title = f"✓  Scan Complete — No Fresh Signals Today"

    lines = []
    for r in results:
        if r.get("error"):
            lines.append(f"**{r['ticker']}** ⚠ error")
            continue

        # Signal badge
        if r.get("signal_stale"):
            sig = "⚠ stale"
        elif r.get("entry_signal"):
            sig = "✦ **ENTRY**"
        elif r.get("macd_cross_up"):
            sig = "↑ MACD"
        else:
            sig = "—"

        # Squeeze badge
        sqz = ""
        if r.get("sqz_fired"):
            sqz = " 🔥SQZ"
        elif r.get("sqz_on"):
            sqz = " ⚡SQZ"

        pct_color = "+" if r["today_chg_pct"] >= 0 else ""
        lines.append(
            f"**{r['ticker']}** `${r['price']:.2f}` {pct_color}{r['today_chg_pct']:.1f}%  "
            f"MTF {r['mtf_score']}/3  RSI {r['rsi']:.0f}  {sig}{sqz}"
        )

    # Discord has a 4096 char embed description limit — trim if needed
    description = "\n".join(lines)
    if len(description) > 4000:
        description = description[:3900] + "\n*(trimmed — use web app for full results)*"

    embed = discord.Embed(title=title, description=description, color=color, timestamp=_ts())
    embed.set_footer(
        text=f"Conquest  •  {len(entries)} entries  •  {len(crosses)} MACD  •  "
             f"{len(errors)} errors  •  {len(watchlist)} tickers  •  Not financial advice"
    )
    await thinking.delete()
    await ctx.send(embed=embed)


# ── !analyze ──────────────────────────────────────────────────────────────────

@bot.command(name="analyze", aliases=["a", "check"])
async def analyze_cmd(ctx, ticker: str = ""):
    if not ticker:
        await ctx.send("Usage: `!analyze AAPL`")
        return

    ticker   = ticker.upper()
    thinking = await ctx.send(f"⏳ Analyzing **{ticker}** with Conquest Brain...")

    def _do_analyze():
        from alerts.scanner  import scan_ticker
        from conquest_brain  import analyze_signal
        r    = scan_ticker(ticker)
        note = analyze_signal(r) if not r.get("error") else None
        return r, note

    r, note = await _run_sync(_do_analyze)

    if r.get("error"):
        await thinking.delete()
        await ctx.send(f"⚠ Error analyzing **{ticker}**: {r['error'][:200]}")
        return

    # Color and signal label
    if r.get("signal_stale"):
        color = COLOR_RED
        sig   = "⚠ STALE — Moving against signal today"
    elif r.get("entry_signal"):
        color = COLOR_GREEN
        sig   = "✦ ENTRY SIGNAL — All conditions met"
    elif r.get("macd_cross_up"):
        color = COLOR_ORANGE
        sig   = "↑ MACD Cross — Momentum shifting"
    else:
        color = COLOR_DARK
        sig   = "— No active signal"

    chg_sign = "+" if r["today_chg_pct"] >= 0 else ""
    desc = (
        f"**Price:** ${r['price']:.2f}  ({chg_sign}{r['today_chg_pct']:.1f}% today)\n"
        f"**Signal:** {sig}\n"
        f"**MTF Score:** {r['mtf_score']}/3  —  "
        f"M:{r['monthly']}  W:{r['weekly']}  D:{r['daily']}\n"
        f"**RSI:** {r['rsi']:.0f}  **ADX:** {r['adx']:.0f}  **HV Rank:** {r['hv_rank']:.0f}/100\n"
    )

    if r.get("sqz_fired"):
        desc += "\n🔥 **TTM Squeeze just fired** — stock broke out of a low-vol coil"
    elif r.get("sqz_on"):
        desc += "\n⚡ **TTM Squeeze active** — stock coiling, watch for directional break"

    if note:
        desc += f"\n\n⚔️ *{note}*"

    embed = discord.Embed(
        title=f"📊  {ticker} Deep Dive",
        description=desc,
        color=color,
        timestamp=_ts(),
    )
    embed.set_footer(text="Conquest Trading  •  Not financial advice  •  Always DYOR")
    await thinking.delete()
    await ctx.send(embed=embed)


# ── !portfolio ────────────────────────────────────────────────────────────────

@bot.command(name="portfolio", aliases=["port", "p", "positions"])
async def portfolio_cmd(ctx):
    thinking = await ctx.send("⏳ Loading live portfolio...")

    def _do_portfolio():
        from positions import get_positions_web_data
        return get_positions_web_data()

    data = await _run_sync(_do_portfolio)

    if not data or not data.get("positions"):
        await thinking.delete()
        await ctx.send(
            "📋 No open positions.\n"
            "Add one at http://localhost:5000/positions"
        )
        return

    total_pnl = data.get("total_pnl",     0)
    total_cost = data.get("total_cost",   0)
    total_pct  = data.get("total_pnl_pct", 0) * 100
    vix        = data.get("vix", 0)
    sign       = "+" if total_pnl >= 0 else ""
    color      = COLOR_GREEN if total_pnl >= 0 else COLOR_RED

    lines = []
    for p in data["positions"]:
        if p.get("error"):
            lines.append(f"**{p['ticker']}** ⚠ {str(p['error'])[:60]}")
            continue

        pnl   = p.get("pnl",     0)
        pnl_p = p.get("pnl_pct", 0) * 100
        ps    = "+" if pnl >= 0 else ""
        rec   = p.get("status", "HOLD")

        if p.get("kind") == "spread":
            lines.append(
                f"**{p['ticker']}** {p.get('type_label','Spread')}  "
                f"`${p['long_strike']:.0f}/{p['short_strike']:.0f}` exp {p['expiry']} ({p['dte']}d)  "
                f"**{ps}${pnl:.0f}** ({ps}{pnl_p:.1f}%)  `{rec}`"
            )
        elif p.get("kind") == "option":
            lines.append(
                f"**{p['ticker']}** {p['option_type'].upper()} ${p['strike']}  "
                f"exp {p['expiry']} ({p['dte']}d)  "
                f"**{ps}${pnl:.0f}** ({ps}{pnl_p:.1f}%)  `{rec}`"
            )
        elif p.get("kind") == "stock":
            lines.append(
                f"**{p['ticker']}** {p.get('shares',0):.0f}sh  "
                f"entry ${p.get('entry_price',0):.2f} → ${p.get('current_price',0):.2f}  "
                f"**{ps}${pnl:.0f}** ({ps}{pnl_p:.1f}%)  `{rec}`"
            )

    embed = discord.Embed(
        title=f"📋  Paper Portfolio — {sign}${total_pnl:,.0f} ({sign}{total_pct:.1f}%)",
        description="\n".join(lines) or "No position data.",
        color=color,
        timestamp=_ts(),
    )
    embed.add_field(name="Cost Basis",  value=f"${total_cost:,.0f}", inline=True)
    embed.add_field(name="Total P&L",   value=f"{sign}${total_pnl:,.0f}", inline=True)
    vix_note = data.get("vix_note", "")
    embed.add_field(name=f"VIX  {vix}", value=vix_note, inline=True)
    embed.set_footer(text="Conquest Trading  •  Paper Portfolio  •  Black-Scholes pricing")
    await thinking.delete()
    await ctx.send(embed=embed)


# ── !pnl ──────────────────────────────────────────────────────────────────────

@bot.command(name="pnl")
async def pnl_cmd(ctx):
    thinking = await ctx.send("⏳ Calculating daily P&L...")

    def _do_pnl():
        from positions              import get_positions_web_data
        from alerts.positions_notifier import notify_daily_pnl
        data = get_positions_web_data()
        ok   = notify_daily_pnl(data)
        return ok

    ok = await _run_sync(_do_pnl)
    await thinking.delete()
    if ok:
        await ctx.send("📊 Daily P&L summary sent to your positions channel!")
    else:
        await ctx.send(
            "⚠ Couldn't send — add a Positions Webhook URL on the Portfolio page first.\n"
            "http://localhost:5000/positions"
        )


# ── !briefing ─────────────────────────────────────────────────────────────────

@bot.command(name="briefing", aliases=["brief", "morning", "b"])
async def briefing_cmd(ctx):
    """Fetch (or generate) today's Morning Intelligence Brief."""
    s         = _load_settings()
    watchlist = s.get("watchlist", "").split()

    thinking = await ctx.send(
        "⚔️ Pulling market data, FRED macro, and generating intelligence brief... (~20 seconds)"
    )

    def _do_brief():
        from morning_brief import generate_brief
        return generate_brief(watchlist=watchlist)

    brief = await _run_sync(_do_brief)

    await thinking.delete()

    sections        = brief.get("sections", {})
    discord_summary = brief.get("discord_summary", "")
    snapshot        = brief.get("snapshot", {})
    sectors         = brief.get("sector_rotation", [])

    # Description: use discord_summary, or fall back to first section
    description = discord_summary
    if not description and sections.get("macro_regime"):
        description = sections["macro_regime"][:800]
    if not description:
        description = "Brief generated — see sections below."

    color = COLOR_PURPLE
    embed = discord.Embed(
        title="⚔️  Conquest Morning Intelligence Brief",
        description=description,
        color=color,
        timestamp=_ts(),
    )

    # Key market numbers
    spy = snapshot.get("SPY", {})
    qqq = snapshot.get("QQQ", {})
    vix = snapshot.get("^VIX", {})
    tny = snapshot.get("^TNX", {})
    oil = snapshot.get("CL=F", {})
    uup = snapshot.get("UUP", {})

    mkt_lines = []
    for label, s_data in [("SPY", spy), ("QQQ", qqq), ("VIX", vix),
                           ("10Y", tny), ("Oil", oil), ("Dollar", uup)]:
        if s_data:
            sign = "▲" if s_data["chg"] > 0 else ("▼" if s_data["chg"] < 0 else "–")
            mkt_lines.append(f"{sign} **{label}** {s_data['price']} ({s_data['chg']:+.2f}%)")

    if mkt_lines:
        embed.add_field(
            name="Market",
            value="  ".join(mkt_lines[:3]) + "\n" + "  ".join(mkt_lines[3:]),
            inline=False,
        )

    # Sector rotation — top 3 and bottom 3
    if sectors:
        top = "  ".join(
            f"{'▲' if d['ret5'] > 0 else '▼'} {d['name']} {d['ret5']:+.1f}%"
            for d in sectors[:3]
        )
        bot = "  ".join(
            f"▼ {d['name']} {d['ret5']:+.1f}%"
            for d in sectors[-3:]
        )
        embed.add_field(name="🟢 Leading (5d)",  value=top, inline=True)
        embed.add_field(name="🔴 Lagging (5d)",  value=bot, inline=True)

    # What to watch section (if we have it)
    if sections.get("what_to_watch"):
        # First sentence only for Discord
        watch_short = sections["what_to_watch"].split(".")[0] + "."
        embed.add_field(name="👁 Watch Today", value=watch_short[:300], inline=False)

    gen_at = brief.get("generated_at", "")[:16].replace("T", " ")
    embed.set_footer(
        text=f"Conquest Intelligence Desk  •  {gen_at} UTC  •  Full brief at /brief"
    )
    await ctx.send(embed=embed)


# ── !macro ────────────────────────────────────────────────────────────────────

@bot.command(name="macro", aliases=["m", "fed"])
async def macro_cmd(ctx):
    thinking = await ctx.send("⏳ Fetching FRED macro data...")

    def _do_macro():
        from macro.fred_data import fetch_fred_macro
        from macro.fetcher   import fetch_macro_data, macro_health_score, sector_rotation_phase
        fred              = fetch_fred_macro()
        mkt               = fetch_macro_data()
        score, max_score  = macro_health_score(mkt)
        phase, desc, secs = sector_rotation_phase(mkt)
        return fred, score, max_score, phase, desc, secs

    fred, score, max_score, phase, desc, secs = await _run_sync(_do_macro)

    bar   = "█" * score + "░" * (max_score - score)
    grade = "FAVORABLE" if score >= 4 else ("NEUTRAL" if score >= 2 else "CAUTION")
    color = COLOR_GREEN   if score >= 4 else (COLOR_ORANGE if score >= 2 else COLOR_RED)

    fred_parts = []
    for sid, r in fred.items():
        if r.get("error") or r.get("latest") is None:
            continue
        val = r["latest"]
        if sid == "GDPC1":
            qoq = r.get("qoq") or 0
            fred_parts.append(f"GDP: **${val:,.0f}B** ({qoq:+.1f}% ann.)")
        elif sid == "CPIAUCSL":
            yoy = r.get("yoy") or 0
            fred_parts.append(f"CPI: **{val:.1f}** ({yoy:+.1f}% YoY)")
        elif sid == "FEDFUNDS":
            fred_parts.append(f"Fed Funds: **{val:.2f}%**")
        elif sid == "DGS10":
            fred_parts.append(f"10Y Treasury: **{val:.2f}%**")
        elif sid == "T10Y2Y":
            status = "normal" if val >= 0 else "⚠ **INVERTED**"
            fred_parts.append(f"Yield Curve: **{val:+.2f}%** ({status})")
        elif sid == "UNRATE":
            fred_parts.append(f"Unemployment: **{val:.1f}%**")
        elif sid == "UMCSENT":
            fred_parts.append(f"Sentiment: **{val:.1f}**")

    embed = discord.Embed(
        title=f"🌍  Macro Snapshot — {score}/{max_score}  [{bar}]  {grade}",
        color=color,
        timestamp=_ts(),
    )
    embed.add_field(
        name="Federal Reserve (FRED)",
        value="\n".join(fred_parts) or "Data unavailable",
        inline=False,
    )
    embed.add_field(
        name=f"Economic Phase: {phase}",
        value=f"{desc}\n**Best sectors:** {', '.join(secs[:3])}",
        inline=False,
    )
    embed.set_footer(text="Conquest Trading  •  Data: Federal Reserve FRED  •  Not financial advice")
    await thinking.delete()
    await ctx.send(embed=embed)


# ── !stats ────────────────────────────────────────────────────────────────────

@bot.command(name="stats", aliases=["paperstat", "paperstats"])
async def stats_cmd(ctx):
    """Show paper trading performance summary."""
    thinking = await ctx.send("📊 Calculating paper trading stats…")

    def _get():
        from paper_trader import get_paper_stats
        return get_paper_stats()

    s = await _run_sync(_get)

    if not s["closed_count"] and not s["open_count"]:
        await thinking.delete()
        await ctx.send("No paper trades yet. Use `!generate` to create today's 10 trades.")
        return

    color = COLOR_GREEN if (s.get("total_pnl") or 0) >= 0 else COLOR_RED
    embed = discord.Embed(
        title="🧪  Paper Trading Stats",
        color=color,
        timestamp=_ts(),
    )

    # Top-line numbers
    wr_str  = f"{s['win_rate']*100:.1f}%" if s["closed_count"] else "—"
    pnl_str = f"${s['total_pnl']:+.2f}"  if s["closed_count"] else "—"
    sh_str  = str(s["sharpe"])            if s.get("sharpe") is not None else "—"
    pf_str  = str(s["profit_factor"])     if s.get("profit_factor") else "—"

    embed.add_field(name="📊 Overview", value=(
        f"**Trades:** {s['total_trades']} ({s['open_count']} open · {s['closed_count']} closed)\n"
        f"**Win Rate:** {wr_str}   **Total P&L:** {pnl_str}\n"
        f"**Sharpe:** {sh_str}   **Profit Factor:** {pf_str}\n"
        f"**Avg hold:** {s.get('avg_hold', '—')} days"
    ), inline=False)

    # Best / worst
    if s.get("best_trade"):
        b = s["best_trade"]
        embed.add_field(
            name="🏆 Best Trade",
            value=f"**{b['ticker']}** {b['trade_type'].replace('_',' ')} → ${b['pnl']:+.2f} ({b['pnl_pct']*100:.1f}%)",
            inline=True,
        )
    if s.get("worst_trade"):
        w = s["worst_trade"]
        embed.add_field(
            name="💀 Worst Trade",
            value=f"**{w['ticker']}** {w['trade_type'].replace('_',' ')} → ${w['pnl']:+.2f} ({w['pnl_pct']*100:.1f}%)",
            inline=True,
        )

    # By trade type breakdown
    if s.get("by_type"):
        lines = []
        for tt, d in sorted(s["by_type"].items(), key=lambda x: -x[1]["total_pnl"]):
            icon = {"call_spread":"📈","put_spread":"📉","long_call":"🟢","long_put":"🔴",
                    "iron_condor":"🦅","stock_long":"💹","stock_short":"🔻"}.get(tt, "•")
            lines.append(
                f"{icon} **{tt.replace('_',' ')}** — {d['count']} trades · "
                f"{d['win_rate']*100:.0f}% win · avg ${d['avg_pnl']:+.2f}"
            )
        embed.add_field(name="📋 By Type", value="\n".join(lines), inline=False)

    embed.set_footer(text="Conquest Trading  •  Black-Scholes simulation  •  Not financial advice")
    await thinking.delete()
    await ctx.send(embed=embed)


# ── !trades ────────────────────────────────────────────────────────────────────

@bot.command(name="trades", aliases=["today", "papertrades"])
async def trades_cmd(ctx):
    """Show live paper positions in Hidden Wolf style."""
    thinking = await ctx.send("📋 Loading positions…")

    def _get():
        from paper_trader import load_trades, get_paper_stats, run_daily_close
        import yfinance as yf

        # Fresh mark-to-market so numbers are current
        run_daily_close()

        today_str = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
        all_t  = load_trades()
        open_t = [t for t in all_t if t.get("status") == "open"]
        stats  = get_paper_stats()

        # SPY return since earliest open trade (for alpha calc)
        spx_return = 0.0
        try:
            if open_t:
                earliest = min(t.get("date_entered","")[:10] for t in open_t)
                spy = yf.download("SPY", start=earliest, progress=False, auto_adjust=True)
                if not spy.empty and len(spy) >= 2:
                    spx_return = float(spy["Close"].iloc[-1] / spy["Close"].iloc[0] - 1) * 100
        except Exception:
            pass

        total_cost    = sum(abs(t.get("cost_basis", 0)) for t in open_t)
        total_pnl_open= sum(t.get("pnl", 0) for t in open_t)
        today_pnl     = sum(
            t.get("pnl", 0) for t in all_t
            if t.get("date_entered", "").startswith(today_str)
        )

        return open_t, stats, total_cost, total_pnl_open, today_pnl, spx_return

    import pytz
    open_trades, stats, total_cost, total_pnl_open, today_pnl, spx_return = \
        await _run_sync(_get)

    await thinking.delete()

    if not open_trades and not stats["closed_count"]:
        await ctx.send("No paper trades yet. Use `!generate` to create today's batch.")
        return

    # ── Overall return on deployed capital ────────────────────────────────────
    overall_pct = (total_pnl_open / total_cost * 100) if total_cost else 0
    alpha       = overall_pct - spx_return
    win_rate    = stats.get("win_rate", 0) * 100
    all_time_pnl= stats.get("total_pnl", 0)

    color = COLOR_GREEN if total_pnl_open >= 0 else COLOR_RED

    embed = discord.Embed(
        title="⚔️  CONQUEST — LIVE PAPER TRADES",
        color=color,
        timestamp=_ts(),
    )

    # ── Header summary row ────────────────────────────────────────────────────
    embed.add_field(
        name="Total P&L",
        value=f"**${total_pnl_open:+.2f}**\n({overall_pct:+.1f}%)",
        inline=True,
    )
    embed.add_field(
        name="Alpha vs SPY",
        value=f"**{alpha:+.2f}%**\nSPY: {spx_return:+.1f}%",
        inline=True,
    )
    embed.add_field(
        name="Win Rate",
        value=f"**{win_rate:.1f}%**\n{stats['closed_count']} closed",
        inline=True,
    )
    embed.add_field(
        name="Today P&L",
        value=f"**${today_pnl:+.2f}**",
        inline=True,
    )
    embed.add_field(
        name="Positions",
        value=f"**{len(open_trades)}** open",
        inline=True,
    )
    embed.add_field(
        name="All-Time P&L",
        value=f"**${all_time_pnl:+.2f}**",
        inline=True,
    )

    # ── Position lines ────────────────────────────────────────────────────────
    def _conviction(mtf):
        if mtf >= 3:   return "HIGH"
        if mtf >= 2:   return "MEDIUM"
        return "LOW"

    def _direction(trade_type):
        return {
            "stock_long":  "LONG",
            "stock_short": "SHORT",
            "long_call":   "LONG CALL",
            "long_put":    "LONG PUT",
            "call_spread": "CALL SPD",
            "put_spread":  "PUT SPD",
            "iron_condor": "CONDOR",
        }.get(trade_type, trade_type.upper())

    sorted_trades = sorted(open_trades, key=lambda t: t.get("pnl", 0), reverse=True)
    lines = []
    for t in sorted_trades:
        pnl   = t.get("pnl", 0)
        pct   = t.get("pnl_pct", 0) * 100
        dot   = "🟢" if pnl >= 0 else "🔴"
        conv  = _conviction(t.get("mtf_score", 0))
        dirn  = _direction(t["trade_type"])
        weight= (abs(t.get("cost_basis", 0)) / total_cost * 100) if total_cost else 0
        days  = t.get("days_held", 0)
        lines.append(
            f"{dot} **{t['ticker']}** ({dirn}) | "
            f"{pct:+.1f}% | {conv} | {weight:.1f}% | day {days}"
        )

    if lines:
        mid = len(lines) // 2
        if len(lines) <= 5:
            embed.add_field(name="Open Positions", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Open Positions (1–5)",  value="\n".join(lines[:mid]),  inline=False)
            embed.add_field(name="Open Positions (6–10)", value="\n".join(lines[mid:]), inline=False)

    embed.set_footer(text="Updated every 15 min during market hours  •  Conquest Trading  •  BS simulation")
    await ctx.send(embed=embed)


# ── !generate ─────────────────────────────────────────────────────────────────

@bot.command(name="generate", aliases=["gen", "gentrades"])
async def generate_cmd(ctx):
    """Manually trigger today's 10 paper trades."""
    thinking = await ctx.send("⚡ Scanning universe and generating trades…")

    def _gen():
        from paper_trader import generate_daily_trades
        return generate_daily_trades(10)

    new_trades = await _run_sync(_gen)
    await thinking.delete()

    if not new_trades:
        await ctx.send("⚠ Today's trades already exist (or scanner returned no data). Use `!trades` to see them.")
        return

    type_summary = {}
    for t in new_trades:
        tt = t["trade_type"]
        type_summary[tt] = type_summary.get(tt, 0) + 1

    lines = [f"• {tt.replace('_',' ')} ×{cnt}" for tt, cnt in type_summary.items()]
    embed = discord.Embed(
        title=f"🧪  Generated {len(new_trades)} Paper Trades",
        description="\n".join(lines),
        color=COLOR_GREEN,
        timestamp=_ts(),
    )
    embed.add_field(
        name="What's next?",
        value="They'll be marked-to-market at **4:05 PM ET** and closed if they hit stops/targets.\nUse `!trades` to see them. Use `!stats` for running totals.",
        inline=False,
    )
    embed.set_footer(text="Conquest Trading  •  Black-Scholes simulation  •  Not financial advice")
    await ctx.send(embed=embed)


# ── !testchannels ─────────────────────────────────────────────────────────────

@bot.command(name="testchannels", aliases=["tc", "techchannels"])
async def testchannels_cmd(ctx):
    """Fire a test message to every dedicated channel to verify routing."""

    CHANNEL_MAP = [
        ("morning-briefing", "🗞",  "Morning Intelligence Brief",  "Auto-posts the daily macro brief at 9:00 AM ET."),
        ("trade-alerts",     "🧪",  "Trade Alerts",                "New paper trades post here at 9:35 AM ET."),
        ("trade-log",        "📋",  "Trade Log",                   "Each individual stop/target/expiry close posts here."),
        ("evening-debrief",  "📊",  "Evening Debrief",             "Full EOD wrap with today's closed trades at 4:05 PM ET."),
        ("daily-pnl",        "💰",  "Daily P&L",                   "Short P&L one-liner posts here at 4:05 PM ET."),
        ("watchlist",        "📡",  "Watchlist Signals",           "Use !scan to post scan results here."),
        ("macro-worldview",  "🌍",  "Macro Worldview",             "Use !macro to post the macro snapshot here."),
        ("live-positions",   "📈",  "Live Positions",              "Use !trades to see live paper positions."),
        ("status-dashboard", "🏆",  "Status Dashboard",            "Use !stats to post performance summary here."),
    ]

    status_lines = []
    found_count  = 0

    for channel_name, icon, label, description in CHANNEL_MAP:
        ch = await _get_channel(channel_name)

        if ch:
            found_count += 1
            status_lines.append(f"✅  **#{channel_name}** — found")
            try:
                test_embed = discord.Embed(
                    title=f"{icon}  Channel Test — {label}",
                    description=(
                        f"{description}\n\n"
                        f"✅ **Routing confirmed.** This channel is correctly wired to Conquest Bot."
                    ),
                    color=COLOR_GREEN,
                    timestamp=_ts(),
                )
                test_embed.set_footer(text="Conquest Trading  •  Channel routing test  •  !testchannels")
                await ch.send(embed=test_embed)
            except discord.Forbidden:
                status_lines[-1] = f"⚠️  **#{channel_name}** — found but **no permission to post**"
        else:
            status_lines.append(f"❌  **#{channel_name}** — not found (create this channel)")

    # Summary back to whoever ran the command
    color   = COLOR_GREEN if found_count == len(CHANNEL_MAP) else (COLOR_ORANGE if found_count > 0 else COLOR_RED)
    summary = discord.Embed(
        title=f"⚔️  Channel Routing Test — {found_count}/{len(CHANNEL_MAP)} found",
        description="\n".join(status_lines),
        color=color,
        timestamp=_ts(),
    )
    summary.add_field(
        name="What to do if a channel shows ❌",
        value=(
            "Create that channel in your Discord server with the exact name shown.\n"
            "The bot finds channels by name automatically — no IDs or webhooks needed."
        ),
        inline=False,
    )
    summary.set_footer(text="Conquest Trading  •  Check each channel for a test message")
    await ctx.send(embed=summary)


# ── Auto morning briefing — 9:00 AM ET, Mon–Fri ───────────────────────────────

# ── Shared state for auto tasks ───────────────────────────────────────────────
_briefing_sent_date    = None   # date of last auto morning briefing
_paper_generated_dates = set()  # dates paper trades were already generated
_paper_notified_ids    = set()  # closed trade IDs already posted to Discord
_paper_eod_dates       = set()  # dates EOD summary already posted


async def _get_channel(primary: str, *fallbacks: str):
    """
    Find a Discord text channel by name.
    Tries `primary` first, then each fallback in order, then gives up.

    Usage:
        ch = await _get_channel("morning-briefing", "general")
        ch = await _get_channel("trade-log", "trade-alerts")
    """
    s = _load_settings()

    # 1. Check settings for a saved channel ID matching the primary name
    id_key = f"ch_{primary.replace('-', '_')}"  # e.g. "ch_morning_briefing"
    saved_id = s.get(id_key) or s.get("bot_alerts_channel_id")
    if saved_id:
        ch = bot.get_channel(int(saved_id))
        if ch:
            return ch

    # 2. Search all guilds by exact channel name (primary, then fallbacks)
    names_to_try = [primary] + list(fallbacks)
    for name in names_to_try:
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if ch.name.lower() == name.lower():
                    return ch

    return None


# Legacy alias so any code that still calls _get_alert_channel() still works
async def _get_alert_channel():
    return await _get_channel("trade-alerts", "conquest-alerts", "general")


# ── Fully-automated paper trading loop ────────────────────────────────────────
# Runs every 15 minutes.  No manual interaction needed at all.
#
#  9:35 AM ET  → generate 10 trades, post summary to Discord
#  Every 15 min during market hours → mark-to-market, close stops/targets,
#                                     post close notification for each one
#  4:05 PM ET  → final mark + EOD daily summary

@tasks.loop(minutes=15)
async def paper_trading_loop():
    global _paper_generated_dates, _paper_notified_ids, _paper_eod_dates

    try:
        import pytz
        now_et = datetime.now(pytz.timezone("America/New_York"))
        today  = now_et.date()
        h, m   = now_et.hour, now_et.minute

        # Weekdays only
        if now_et.weekday() >= 5:
            return

        # Market window: 9:30 AM – 4:15 PM ET
        after_open  = (h > 9) or (h == 9  and m >= 30)
        before_close= (h < 16) or (h == 16 and m <= 15)
        if not (after_open and before_close):
            return

        # Route each message type to its dedicated channel
        ch_trades   = await _get_channel("trade-alerts",    "general")
        ch_log      = await _get_channel("trade-log",       "trade-alerts", "general")
        ch_eod      = await _get_channel("evening-debrief", "daily-pnl",    "general")
        ch_pnl      = await _get_channel("daily-pnl",       "evening-debrief", "general")

        # ── 1. Generate today's trades (9:35–10:00 AM window) ─────────────────
        if today not in _paper_generated_dates and (h == 9 and m >= 35 or h >= 10):
            _paper_generated_dates.add(today)

            def _gen():
                from paper_trader import generate_daily_trades
                return generate_daily_trades(10)

            new_trades = await _run_sync(_gen)

            if new_trades and ch_trades:
                type_counts: dict = {}
                for t in new_trades:
                    tt = t["trade_type"]
                    type_counts[tt] = type_counts.get(tt, 0) + 1

                breakdown = "  ".join(
                    f"{tt.replace('_',' ')} ×{cnt}"
                    for tt, cnt in sorted(type_counts.items())
                )
                embed = discord.Embed(
                    title=f"🧪  Paper Trades Generated  —  {today.strftime('%b %d')}",
                    description=(
                        f"**{len(new_trades)} trades** placed across the universe.\n"
                        f"{breakdown}\n\n"
                        "Checked every 15 min — closes anything that hits a target or stop.\n"
                        "Use `!trades` to see the full list."
                    ),
                    color=COLOR_GOLD,
                    timestamp=_ts(),
                )
                embed.set_footer(
                    text="Auto-closes: options +50%/−75% · stocks +5%/−3% · max 5 days"
                )
                await ch_trades.send(embed=embed)

        # ── 2. Mark-to-market + close check (every tick during market hours) ──
        def _run_close():
            from paper_trader import run_daily_close, load_trades
            run_daily_close()
            all_t = load_trades()
            # Return trades that closed and haven't been notified yet
            newly = [
                t for t in all_t
                if t.get("status") == "closed"
                and t.get("id") not in _paper_notified_ids
            ]
            return newly

        newly_closed = await _run_sync(_run_close)

        # Post a notification for every newly closed trade
        for t in newly_closed:
            _paper_notified_ids.add(t.get("id", ""))
            if not ch_log:
                continue

            pnl    = t.get("pnl", 0)
            pnl_pct= t.get("pnl_pct", 0) * 100
            reason = t.get("close_reason", "closed")
            won    = pnl >= 0

            if reason == "profit_target":
                icon, label, color = "✅", "PROFIT TARGET HIT", COLOR_GREEN
            elif reason == "stop_loss":
                icon, label, color = "🛑", "STOP LOSS HIT", COLOR_RED
            else:
                icon, label, color = "⏱", "MAX HOLD REACHED", COLOR_ORANGE

            tt_display = t["trade_type"].replace("_", " ").title()
            embed = discord.Embed(
                title=f"{icon}  {label}  —  {t['ticker']} {tt_display}",
                color=color,
                timestamp=_ts(),
            )
            embed.add_field(
                name="Result",
                value=(
                    f"**{'▲' if won else '▼'} ${pnl:+.2f}** ({pnl_pct:+.1f}%)\n"
                    f"Held {t.get('days_held', 0)} day(s)  ·  "
                    f"Cost ${t.get('cost_basis', 0):.0f}"
                ),
                inline=True,
            )
            embed.add_field(
                name="Trade",
                value=(
                    f"Entered: {t.get('date_entered','')[:10]}\n"
                    f"Closed:  {t.get('date_closed','')[:10]}"
                ),
                inline=True,
            )
            embed.set_footer(
                text="Conquest Trading  •  paper simulation  •  not financial advice"
            )
            await ch_log.send(embed=embed)

        # ── 3. EOD summary (4:05–4:20 PM) ─────────────────────────────────────
        if h == 16 and 5 <= m <= 20 and today not in _paper_eod_dates:
            _paper_eod_dates.add(today)

            def _get_stats():
                from paper_trader import get_paper_stats, load_trades
                from datetime import datetime as _dt
                stats = get_paper_stats()
                # Today's closed trades
                today_str = today.strftime("%Y-%m-%d")
                all_t = load_trades()
                today_closed = [
                    t for t in all_t
                    if t.get("status") == "closed"
                    and (t.get("date_closed") or "")[:10] == today_str
                ]
                today_pnl = sum(t.get("pnl", 0) for t in today_closed)
                return stats, today_closed, round(today_pnl, 2)

            stats, today_closed, today_pnl = await _run_sync(_get_stats)

            if ch_eod or ch_pnl:
                color = COLOR_GREEN if today_pnl >= 0 else COLOR_RED

                # Full EOD wrap → #evening-debrief
                eod_embed = discord.Embed(
                    title=f"📊  Paper Trading EOD Wrap  —  {today.strftime('%b %d')}",
                    color=color,
                    timestamp=_ts(),
                )
                eod_embed.add_field(
                    name="Today",
                    value=(
                        f"Closed {len(today_closed)} trades\n"
                        f"Today P&L: **${today_pnl:+.2f}**"
                    ),
                    inline=True,
                )
                eod_embed.add_field(
                    name="All-Time",
                    value=(
                        f"{stats['closed_count']} closed · "
                        f"{stats['win_rate']*100:.1f}% win rate\n"
                        f"Total P&L: **${stats['total_pnl']:+.2f}**\n"
                        f"Sharpe: {stats['sharpe'] or '—'}"
                    ),
                    inline=True,
                )
                if today_closed:
                    winners = sorted(today_closed,
                                     key=lambda t: t.get("pnl", 0), reverse=True)
                    lines = []
                    for t in winners[:5]:
                        sign = "▲" if t.get("pnl", 0) >= 0 else "▼"
                        lines.append(
                            f"{sign} **{t['ticker']}** {t['trade_type'].replace('_',' ')} "
                            f"${t.get('pnl',0):+.2f} · {t.get('close_reason','').replace('_',' ')}"
                        )
                    eod_embed.add_field(
                        name="Today's Closed Trades",
                        value="\n".join(lines) or "None",
                        inline=False,
                    )
                eod_embed.set_footer(
                    text="Next batch generates tomorrow at 9:35 AM ET  •  Conquest Trading"
                )
                if ch_eod:
                    await ch_eod.send(embed=eod_embed)

                # Short P&L line → #daily-pnl (separate channel)
                if ch_pnl and ch_pnl != ch_eod:
                    sign = "▲" if today_pnl >= 0 else "▼"
                    pnl_embed = discord.Embed(
                        title=f"{sign}  Daily P&L  —  {today.strftime('%b %d')}",
                        description=(
                            f"**Today:** ${today_pnl:+.2f}  ({len(today_closed)} trades closed)\n"
                            f"**All-Time:** ${stats['total_pnl']:+.2f}  "
                            f"({stats['win_rate']*100:.1f}% win rate)"
                        ),
                        color=color,
                        timestamp=_ts(),
                    )
                    pnl_embed.set_footer(text="Conquest Trading  •  Paper Simulation")
                    await ch_pnl.send(embed=pnl_embed)

    except Exception as e:
        print(f"[PaperLoop] Error: {e}")


@paper_trading_loop.before_loop
async def before_paper_loop():
    await bot.wait_until_ready()


# ── Auto morning briefing ─────────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def morning_briefing_task():
    """Fires every minute, posts briefing once at 9 AM ET on weekdays."""
    global _briefing_sent_date
    try:
        import pytz
        now_et = datetime.now(pytz.timezone("America/New_York"))
        today  = now_et.date()

        # Only run Mon–Fri at 9:00 AM, and only once per calendar day
        if not (now_et.weekday() < 5 and now_et.hour == 9 and now_et.minute == 0):
            return
        if _briefing_sent_date == today:
            return

        s = _load_settings()
        if not s.get("auto_briefing"):
            return   # user hasn't enabled auto-briefing

        _briefing_sent_date = today

        channel = await _get_channel("morning-briefing", "general")
        if not channel:
            print("[Bot] Auto-briefing: no channel found. "
                  "Create a #morning-briefing channel or set bot_alerts_channel_id.")
            return

        watchlist = s.get("watchlist", "").split()

        await channel.send("⚔️ Generating morning intelligence brief...")

        def _do_auto_briefing():
            from morning_brief import generate_brief
            return generate_brief(watchlist=watchlist)

        brief = await _run_sync(_do_auto_briefing)

        sections        = brief.get("sections", {})
        discord_summary = brief.get("discord_summary", "")
        snapshot        = brief.get("snapshot", {})
        sectors         = brief.get("sector_rotation", [])

        description = discord_summary
        if not description and sections.get("macro_regime"):
            description = sections["macro_regime"][:800]
        if not description:
            description = "Morning brief generated — check /brief for full details."

        embed = discord.Embed(
            title="⚔️  Conquest Intelligence Brief  —  Auto 9 AM ET",
            description=description,
            color=COLOR_PURPLE,
            timestamp=_ts(),
        )

        # Key numbers
        mkt_lines = []
        for key, label in [("SPY","SPY"),("QQQ","QQQ"),("^VIX","VIX"),
                            ("^TNX","10Y"),("CL=F","Oil"),("UUP","Dollar")]:
            s_data = snapshot.get(key, {})
            if s_data:
                sign = "▲" if s_data["chg"] > 0 else ("▼" if s_data["chg"] < 0 else "–")
                mkt_lines.append(f"{sign} **{label}** {s_data['price']} ({s_data['chg']:+.2f}%)")
        if mkt_lines:
            half = len(mkt_lines) // 2
            embed.add_field(
                name="Market",
                value=("  ".join(mkt_lines[:half]) + "\n" + "  ".join(mkt_lines[half:])).strip(),
                inline=False,
            )

        # Sector rotation
        if sectors:
            top = "  ".join(
                f"{'▲' if d['ret5'] > 0 else '▼'} {d['name']} {d['ret5']:+.1f}%"
                for d in sectors[:3]
            )
            bot = "  ".join(
                f"▼ {d['name']} {d['ret5']:+.1f}%"
                for d in sectors[-3:]
            )
            embed.add_field(name="🟢 Leading (5d)", value=top, inline=True)
            embed.add_field(name="🔴 Lagging (5d)", value=bot, inline=True)

        if sections.get("what_to_watch"):
            watch_short = sections["what_to_watch"].split(".")[0] + "."
            embed.add_field(name="👁 Watch Today", value=watch_short[:300], inline=False)

        embed.set_footer(
            text=f"Conquest Intelligence Desk  •  Full brief at /brief  •  Not financial advice"
        )
        await channel.send(embed=embed)
        print(f"[Bot] Auto morning brief posted at {now_et.strftime('%Y-%m-%d %H:%M ET')}")

    except Exception as e:
        print(f"[Bot] Auto-briefing task error: {e}")


@morning_briefing_task.before_loop
async def before_morning_briefing():
    await bot.wait_until_ready()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    token = _get_token()

    if not token:
        print("\n" + "=" * 60)
        print("  ⚠  DISCORD_BOT_TOKEN not set in .env")
        print("=" * 60)
        print()
        print("  SETUP STEPS:")
        print()
        print("  1. Go to: discord.com/developers/applications")
        print("  2. Click 'New Application' → name it 'Conquest Bot'")
        print("  3. Click 'Bot' in the left menu → 'Add Bot'")
        print("  4. Under 'Token' → click 'Reset Token' → Copy it")
        print("  5. Under 'Privileged Gateway Intents':")
        print("     ✓ Enable 'Message Content Intent'")
        print("  6. Click 'OAuth2' → 'URL Generator'")
        print("     ✓ Scopes: bot")
        print("     ✓ Bot Permissions: Send Messages, Embed Links,")
        print("       Read Messages/View Channels, Read Message History")
        print("  7. Copy the URL at the bottom → paste in browser")
        print("     → Select your server → Authorize")
        print()
        print("  8. Add to your .env file:")
        print("     DISCORD_BOT_TOKEN=paste-your-token-here")
        print()
        print("  Then run:  python discord_bot.py")
        print("=" * 60 + "\n")
        sys.exit(1)

    print("⚔️  Starting Conquest Trading Bot...")
    print("   Press Ctrl+C to stop.\n")
    bot.run(token)
