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
  Morning briefing fires at 9:00 AM ET Mon–Fri automatically
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
    from db import kv_get
    data = kv_get("settings")
    return data if isinstance(data, dict) else {}


def _save_settings(data: dict):
    from db import kv_set
    kv_set("settings", data)


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
        "`!trades` — all open paper positions with live P&L\n"
        "`!portfolio` — same as !trades\n"
        "`!pnl` — today's closed trades + P&L summary"
    ), inline=False)
    embed.add_field(name="🧪  Paper Trading Stats", value=(
        "`!stats` — win rate, P&L, Sharpe, by-type breakdown\n"
        "`!trades` — today's 10 auto-generated paper trades\n"
        "`!generate` — manually trigger today's 10 trades\n"
        "`!agents` — view multi-agent brain weights & learning progress"
    ), inline=False)
    embed.add_field(name="👁  Watchlist & Research", value=(
        "`!watch TICKER` — add to watchlist with thesis, conviction, price targets\n"
        "`!deepdive TICKER` — full 6-prompt deep research report\n"
        "`!watchlist` — show all watched names\n"
        "`!remove TICKER` — drop from watchlist\n"
        "`!screener` — screen watchlist for undervalued stocks (Value/Growth Score)\n"
        "`!earnings` — upcoming earnings for all watchlist names (next 14 days)"
    ), inline=False)
    embed.add_field(name="⚔️  Intelligence", value=(
        "`!briefing` — morning briefing with FRED macro data\n"
        "`!macro` — quick Fed macro snapshot"
    ), inline=False)
    embed.add_field(name="🤖  AI Assistant  (works in ANY channel)", value=(
        "`?your question` — fastest, works everywhere, e.g. `?what is delta`\n"
        "`!ask <question>` — explicit command form\n"
        "**@mention** the bot — mention it anywhere\n"
        "`#conquest-ai` — freeform chat, no prefix needed"
    ), inline=False)
    embed.add_field(name="⚙️  Settings & Diagnostics", value=(
        "Go to the **Alerts** page to toggle the 9 AM auto-briefing on/off.\n"
        "`!testchannels` — verify all 11 channels are wired correctly\n\n"
        "**What runs automatically (no commands needed):**\n"
        "9:00 AM → Brief · Macro · Earnings Radar · (Screener on Mondays)\n"
        "9:30 AM → Market Open Scan → `#watchlist`\n"
        "10:00 AM → Auto-discovery: new entry signals get full AI thesis → `#watchlist`\n"
        "9:35 AM → Paper trades generated\n"
        "12:00 + 3:30 PM → Positions snapshot → `#live-positions`\n"
        "4:05 PM → EOD wrap + Claude debrief + Stats"
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
    """Alias for !trades — shows all open paper positions."""
    await trades_cmd(ctx)


# ── !pnl ──────────────────────────────────────────────────────────────────────

@bot.command(name="pnl")
async def pnl_cmd(ctx):
    """Show today's paper trading P&L summary."""
    thinking = await ctx.send("💰 Calculating today's P&L…")

    def _do_pnl():
        import pytz
        from paper_trader import load_trades, get_paper_stats
        today_str  = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
        all_t      = load_trades()
        today_closed = [
            t for t in all_t
            if t.get("status") == "closed"
            and (t.get("date_closed") or "")[:10] == today_str
        ]
        today_pnl = round(sum(t.get("pnl", 0) for t in today_closed), 2)
        stats     = get_paper_stats()
        return today_closed, today_pnl, stats

    today_closed, today_pnl, stats = await _run_sync(_do_pnl)
    await thinking.delete()

    import pytz
    today_str = datetime.now(pytz.timezone("America/New_York")).strftime("%b %d")
    sign  = "▲" if today_pnl >= 0 else "▼"
    color = COLOR_GREEN if today_pnl >= 0 else COLOR_RED

    lines = []
    for t in sorted(today_closed, key=lambda x: x.get("pnl", 0), reverse=True)[:8]:
        dot = "🟢" if t.get("pnl", 0) >= 0 else "🔴"
        lines.append(
            f"{dot} **{t['ticker']}** {t['trade_type'].replace('_',' ')} "
            f"${t.get('pnl', 0):+.2f} · {t.get('close_reason','').replace('_',' ')}"
        )

    embed = discord.Embed(
        title=f"{sign}  Daily P&L — {today_str}",
        description="\n".join(lines) or "No trades closed today.",
        color=color,
        timestamp=_ts(),
    )
    embed.add_field(name="Today",    value=f"**${today_pnl:+.2f}**  ({len(today_closed)} trades)", inline=True)
    embed.add_field(name="All-Time", value=f"**${stats.get('total_pnl', 0):+.2f}**", inline=True)
    embed.add_field(name="Win Rate", value=f"**{stats.get('win_rate', 0)*100:.1f}%**", inline=True)
    embed.set_footer(text="Conquest Trading  •  Paper Simulation")
    await ctx.send(embed=embed)


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


# ── Macro helper (shared by !macro command and auto morning task) ─────────────

async def _post_macro_embed(channel):
    """Fetch FRED macro data and post the macro snapshot embed to `channel`."""
    def _do():
        from macro.fred_data import fetch_fred_macro
        from macro.fetcher   import fetch_macro_data, macro_health_score, sector_rotation_phase
        fred             = fetch_fred_macro()
        mkt              = fetch_macro_data()
        score, max_score = macro_health_score(mkt)
        phase, desc, secs= sector_rotation_phase(mkt)
        return fred, score, max_score, phase, desc, secs

    fred, score, max_score, phase, desc, secs = await _run_sync(_do)

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
    await channel.send(embed=embed)


# ── !macro ────────────────────────────────────────────────────────────────────

@bot.command(name="macro", aliases=["m", "fed"])
async def macro_cmd(ctx):
    thinking = await ctx.send("⏳ Fetching FRED macro data...")
    await thinking.delete()
    await _post_macro_embed(ctx.channel)


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


# ── !agents ───────────────────────────────────────────────────────────────────

@bot.command(name="agents", aliases=["agentweights", "brains", "swarm"])
async def agents_cmd(ctx):
    """Show the multi-agent trading brain: current weights, vote counts, learning state."""
    thinking = await ctx.send("⚡ Loading agent system status…")

    def _get():
        from conquest_agents import get_agent_system
        return get_agent_system().get_status()

    status = await _run_sync(_get)
    await thinking.delete()

    if status.get("error"):
        await ctx.send(f"⚠ Agent system error: {status['error']}")
        return

    weights      = status.get("weights", {})
    per_agent    = status.get("per_agent", {})
    total_logged = status.get("total_logged", 0)

    AGENT_ICONS = {
        "market_scanner": "📡",
        "valuation":       "💰",
        "technicals":      "📈",
        "catalysts":       "⚡",
        "risk":            "🛡️",
        "options_flow":    "🌊",
    }

    lines = []
    for name, data in per_agent.items():
        w     = data.get("weight", 1.0)
        votes = data.get("votes", 0)
        icon  = AGENT_ICONS.get(name, "•")
        # Visual weight bar (range 0.4–2.2 mapped to 10 blocks)
        filled = int(max(0, min(10, (w - 0.40) / (2.20 - 0.40) * 10)))
        bar    = "█" * filled + "░" * (10 - filled)
        trend  = "▲" if w > 1.05 else ("▼" if w < 0.95 else "–")
        lines.append(
            f"{icon} **{name.replace('_', ' ').title()}**\n"
            f"  `{bar}` **{w:.2f}**  {trend}  ·  {votes} votes logged"
        )

    embed = discord.Embed(
        title="⚡  Conquest Multi-Agent Brain",
        description=(
            f"**6 specialist agents** run in parallel on every ticker.\n"
            f"Weights shift after each trade closes — correct agents earn trust, wrong ones lose it.\n"
            f"**{total_logged}** trades in memory."
        ),
        color=COLOR_PURPLE,
        timestamp=_ts(),
    )
    embed.add_field(
        name="Agent Weights  (default 1.0  ·  range 0.4 – 2.2)",
        value="\n".join(lines) or "No data yet — generate trades to start learning.",
        inline=False,
    )
    embed.add_field(
        name="How It Works",
        value=(
            "• Needs **4 of 6** agents agreeing + weighted confidence ≥ 0.62 to trade\n"
            "• 🛡️ Risk agent can **veto** any trade if confidence < 0.30\n"
            "• Win → correct agents gain **+0.08** weight\n"
            "• Loss → incorrect agents lose **−0.08** weight\n"
            "• Over time: agents that read the market correctly dominate the vote"
        ),
        inline=False,
    )
    embed.set_footer(text="Conquest Multi-Agent System  •  Ruflo-inspired architecture  •  Learning in real-time")
    await ctx.send(embed=embed)


# ── Watchlist commands ────────────────────────────────────────────────────────

def _conviction_color(conviction: str) -> int:
    return {
        "HIGH":   COLOR_GREEN,
        "MEDIUM": COLOR_GOLD,
        "LOW":    COLOR_RED,
    }.get(conviction.upper(), COLOR_DARK)


def _conviction_icon(conviction: str) -> str:
    return {"HIGH": "✦", "MEDIUM": "◆", "LOW": "◇"}.get(conviction.upper(), "•")


def _build_watchlist_embed(entry: dict) -> discord.Embed:
    """Build the #watchlist style embed from an entry dict."""
    ticker     = entry.get("ticker", "?")
    conviction = entry.get("conviction", "MEDIUM").upper()
    color      = _conviction_color(conviction)
    icon       = _conviction_icon(conviction)
    name       = entry.get("name", ticker)
    sector     = entry.get("sector", "")
    price      = entry.get("price", 0)

    embed = discord.Embed(
        title=f"👁  WATCHLIST: {ticker}",
        description=f"*{name}*  •  {sector}  •  ${price:.2f}",
        color=color,
        timestamp=_ts(),
    )

    # Thesis
    thesis = entry.get("thesis", "")
    hook   = entry.get("narrative_hook", "")
    thesis_text = thesis
    if hook and hook not in thesis:
        thesis_text += f"\n\n*{hook}*"
    if thesis_text:
        embed.add_field(name="Thesis", value=thesis_text[:900], inline=False)

    # Conviction + Waiting For (side by side)
    embed.add_field(
        name="Conviction",
        value=f"**{conviction}** {icon}",
        inline=True,
    )
    waiting = entry.get("waiting_for", "—")
    embed.add_field(name="Waiting For", value=waiting[:200], inline=True)

    # Price targets
    bear = entry.get("bear_target", "")
    base = entry.get("base_target", "")
    bull = entry.get("bull_target", "")
    entry_z = entry.get("entry_zone", "")
    stop    = entry.get("hard_stop", "")

    if any([bear, base, bull]):
        targets = []
        if bear: targets.append(f"🔴 Bear: {bear}")
        if base: targets.append(f"🟡 Base: {base}")
        if bull: targets.append(f"🟢 Bull: {bull}")
        if entry_z: targets.append(f"📍 Entry: {entry_z}")
        if stop:    targets.append(f"🛑 Stop: {stop}")
        embed.add_field(name="Price Targets", value="\n".join(targets), inline=False)

    # Risks
    flags = entry.get("risk_flags", [])
    if flags:
        embed.add_field(
            name="⚠ Risk Flags",
            value="\n".join(f"• {f}" for f in flags[:3]),
            inline=False,
        )

    # Analyst consensus
    rec   = entry.get("recommendation", "").upper()
    tgt   = entry.get("target_mean")
    count = entry.get("analyst_count")
    if rec or tgt:
        consensus = f"{rec}"
        if tgt:   consensus += f"  |  Mean target ${tgt:.2f}"
        if count: consensus += f"  ({count} analysts)"
        embed.add_field(name="Analyst Consensus", value=consensus, inline=False)

    embed.set_footer(text="Conquest Watchlist  •  Not financial advice  •  Always DYOR")
    return embed


@bot.command(name="watch", aliases=["w", "addwatch", "addticker"])
async def watch_cmd(ctx, ticker: str = ""):
    """Add a ticker to the watchlist with full AI analysis."""
    if not ticker:
        await ctx.send("Usage: `!watch NVDA`")
        return

    ticker   = ticker.upper()
    thinking = await ctx.send(
        f"👁  Analyzing **{ticker}** — fetching fundamentals, running thesis + "
        f"scenarios + risk report... (~20 seconds)"
    )

    def _run():
        from watchlist_engine import analyze_and_add
        return analyze_and_add(ticker)

    try:
        entry = await _run_sync(_run)
    except Exception as e:
        await thinking.delete()
        await ctx.send(f"⚠ Analysis failed for **{ticker}**: {str(e)[:200]}")
        return

    await thinking.delete()

    embed = _build_watchlist_embed(entry)

    # Post to #stocks channel
    wl_channel = await _get_channel("stocks", "general")
    if wl_channel and wl_channel != ctx.channel:
        await wl_channel.send(embed=embed)
        await ctx.send(
            f"✅ **{ticker}** added to watchlist and posted to {wl_channel.mention}",
            delete_after=10,
        )
    else:
        await ctx.send(embed=embed)


@bot.command(name="deepdive", aliases=["dd", "research"])
async def deepdive_cmd(ctx, ticker: str = ""):
    """5-dimension deep analysis: valuation, technicals, catalysts, risk, options flow."""
    if not ticker:
        await ctx.send("Usage: `!deepdive NVDA`")
        return

    ticker   = ticker.upper()
    thinking = await ctx.send(
        f"🔬  Running 5-dimension analysis on **{ticker}** — "
        f"valuation · technicals · catalysts · risk · options flow… (~30s)"
    )

    def _run():
        from conquest_brain import full_stock_analysis
        return full_stock_analysis(ticker)

    try:
        report = await _run_sync(_run)
    except Exception as e:
        await thinking.delete()
        await ctx.send(f"⚠ Analysis failed for **{ticker}**: {str(e)[:200]}")
        return

    await thinking.delete()

    if report.get("error"):
        await ctx.send(f"⚠ **{ticker}**: {report['error']}")
        return

    price = report.get("price", 0)

    def _verdict_icon(v: str) -> str:
        v = (v or "").upper()
        if v in ("BULLISH","BUY","POSITIVE","UNDERVALUED","LOW"):   return "✅"
        if v in ("BEARISH","AVOID","NEGATIVE","OVERVALUED","HIGH"): return "🔴"
        return "⚠️"

    def _section(name: str, key: str) -> discord.Embed:
        d       = report.get(key, {})
        verdict = d.get("verdict", "—")
        icon    = _verdict_icon(verdict)
        text    = d.get("analysis", "")

        # technicals: fold in signal sub-keys
        if key == "technicals" and "signals" in d:
            sigs = d["signals"]
            text = (
                f"**Trend:** {sigs.get('trend','—')}\n"
                f"**Momentum:** {sigs.get('momentum','—')}\n"
                f"**Strength:** {sigs.get('strength','—')}\n"
                f"**Squeeze:** {sigs.get('squeeze','—')}\n\n"
                + text
            )

        emb = discord.Embed(
            title=f"{icon}  {name}  —  {verdict}",
            description=text[:1800],
            color=COLOR_GREEN if icon == "✅" else (COLOR_RED if icon == "🔴" else COLOR_YELLOW),
            timestamp=_ts(),
        )
        return emb

    COLOR_YELLOW = 0xF59E0B

    # Build all 5 section embeds
    embeds = [
        _section("💰 VALUATION",     "valuation"),
        _section("📈 TECHNICALS",    "technicals"),
        _section("⚡ CATALYSTS",     "catalysts"),
        _section("🛡️ RISK",          "risk"),
        _section("🌊 OPTIONS FLOW",  "options_flow"),
    ]

    # Synthesis embed
    syn   = report.get("synthesis", {})
    overall    = syn.get("overall", "—")
    conviction = syn.get("conviction", "—")
    thesis     = syn.get("thesis", "")
    entry_z    = syn.get("entry_zone", "—")
    stop       = syn.get("stop", "—")
    tgt6       = syn.get("target_6m", "—")
    tgt12      = syn.get("target_12m", "—")
    structure  = syn.get("best_structure", "—")

    syn_color  = COLOR_GREEN if overall == "BUY" else (COLOR_RED if overall == "AVOID" else COLOR_PURPLE)
    syn_embed  = discord.Embed(
        title=f"🎯  SYNTHESIS  —  {overall}  ({conviction} conviction)",
        description=thesis[:1000],
        color=syn_color,
        timestamp=_ts(),
    )
    syn_embed.add_field(name="Entry Zone",      value=entry_z,   inline=True)
    syn_embed.add_field(name="Stop",            value=stop,      inline=True)
    syn_embed.add_field(name="Target 6M / 12M", value=f"{tgt6} / {tgt12}", inline=True)
    syn_embed.add_field(name="Best Structure",  value=structure, inline=False)
    syn_embed.set_footer(text=f"{ticker} @ ${price:.2f}  •  Conquest Intelligence  •  Not financial advice")

    wl_channel = await _get_channel("stocks", "general")
    dest = wl_channel if wl_channel else ctx.channel

    # Header
    header = discord.Embed(
        title=f"📊  {ticker}  —  5-Dimension Deep Analysis",
        description=f"**${price:.2f}**  •  Full intelligence report across all dimensions",
        color=COLOR_PURPLE,
        timestamp=_ts(),
    )
    await dest.send(embed=header)
    for emb in embeds:
        await dest.send(embed=emb)
    await dest.send(embed=syn_embed)

    if dest != ctx.channel:
        await ctx.send(
            f"🔬 Deep dive on **{ticker}** posted to {dest.mention}",
            delete_after=10,
        )


@bot.command(name="watchlist", aliases=["wl", "watched"])
async def watchlist_cmd(ctx):
    """Show all current watchlist entries."""
    def _load():
        from watchlist_engine import load_watchlist
        return load_watchlist()

    entries = await _run_sync(_load)

    if not entries:
        await ctx.send(
            "📋 Watchlist is empty.\n"
            "Use `!watch TICKER` to add a stock with full analysis."
        )
        return

    lines = []
    for e in entries:
        ticker     = e.get("ticker", "?")
        conviction = e.get("conviction", "?")
        price      = e.get("price", 0)
        waiting    = e.get("waiting_for", "")[:60]
        icon       = _conviction_icon(conviction)
        added      = e.get("added_at", "")[:10]
        lines.append(
            f"{icon} **{ticker}** `${price:.2f}`  {conviction}  —  {waiting}  *(added {added})*"
        )

    embed = discord.Embed(
        title=f"👁  Conquest Watchlist  —  {len(entries)} names",
        description="\n".join(lines),
        color=COLOR_PURPLE,
        timestamp=_ts(),
    )
    embed.set_footer(
        text="!watch TICKER to add  •  !remove TICKER to drop  •  !deepdive TICKER for full report"
    )
    await ctx.send(embed=embed)


@bot.command(name="remove", aliases=["unwatch", "drop"])
async def remove_cmd(ctx, ticker: str = ""):
    """Remove a ticker from the watchlist."""
    if not ticker:
        await ctx.send("Usage: `!remove NVDA`")
        return

    ticker = ticker.upper()

    def _do():
        from watchlist_engine import remove_entry
        return remove_entry(ticker)

    removed = await _run_sync(_do)
    if removed:
        await ctx.send(f"✅ **{ticker}** removed from watchlist.")
    else:
        await ctx.send(f"⚠ **{ticker}** wasn't on the watchlist.")


@bot.command(name="screener", aliases=["screen", "undervalued"])
async def screener_cmd(ctx, *tickers):
    """
    Screen tickers for Value/Growth Score (P/S ÷ rev growth).
    Uses your watchlist if no tickers given.
    """
    def _load_wl():
        from watchlist_engine import load_watchlist
        return [e.get("ticker") for e in load_watchlist()]

    if tickers:
        to_screen = [t.upper() for t in tickers]
    else:
        to_screen = await _run_sync(_load_wl)

    if not to_screen:
        await ctx.send(
            "No tickers to screen. Use `!screener AAPL NVDA MSFT` or "
            "add stocks with `!watch TICKER` first."
        )
        return

    # Cap at 10 to avoid very long runs
    to_screen = to_screen[:10]
    thinking  = await ctx.send(
        f"📊  Fetching data and screening **{len(to_screen)}** ticker(s) "
        f"for value vs growth... (~{len(to_screen)*3}s)"
    )

    def _run():
        from watchlist_engine import fetch_ticker_data, build_data_block
        from conquest_brain   import watchlist_screener
        pairs = []
        for t in to_screen:
            try:
                d = fetch_ticker_data(t)
                b = build_data_block(d)
                pairs.append((t, b))
            except Exception:
                pass
        return watchlist_screener(pairs)

    results = await _run_sync(_run)
    await thinking.delete()

    if not results:
        await ctx.send("⚠ Screener returned no results — API or data issue.")
        return

    lines = []
    for r in results:
        t       = r.get("ticker", "?")
        score   = r.get("score")
        verdict = r.get("verdict", "?")
        reason  = r.get("reason", "")[:80]
        score_s = f"{score:.2f}" if score is not None else "N/A"
        icon    = "🟢" if verdict == "UNDERVALUED" else ("🟡" if verdict == "FAIRLY VALUED" else "🔴")
        lines.append(f"{icon} **{t}** score={score_s}  {verdict}\n  *{reason}*")

    embed = discord.Embed(
        title=f"📊  Value/Growth Screener  —  {len(results)} tickers",
        description="\n\n".join(lines),
        color=COLOR_PURPLE,
        timestamp=_ts(),
    )
    embed.add_field(
        name="How to read",
        value="Score = P/S ÷ Revenue Growth %. Lower = more growth per valuation dollar.",
        inline=False,
    )
    embed.set_footer(text="Conquest Screener  •  Not financial advice  •  Always DYOR")
    await ctx.send(embed=embed)


# ── !earnings ─────────────────────────────────────────────────────────────────

@bot.command(name="earnings", aliases=["er", "earningsradar", "calendar"])
async def earnings_cmd(ctx, days: int = 14):
    """Show upcoming earnings for all watchlist names within N days."""
    thinking = await ctx.send(
        f"📅 Checking earnings calendar for your watchlist (next {days} days)..."
    )

    def _run():
        from watchlist_engine import get_upcoming_earnings
        return get_upcoming_earnings(days)

    upcoming = await _run_sync(_run)
    await thinking.delete()

    if not upcoming:
        await ctx.send(
            f"📅 No watchlist names have earnings in the next {days} days.\n"
            f"Try `!earnings 30` to look further out."
        )
        return

    lines = []
    for e in upcoming:
        d      = e["days_to"]
        icon   = _conviction_icon(e.get("conviction", "MEDIUM"))
        urgency = "🔴 " if d <= 2 else ("🟡 " if d <= 7 else "🟢 ")
        date_label = "TODAY" if d == 0 else ("TOMORROW" if d == 1 else f"in {d}d")
        thesis_snip = e.get("thesis", "")[:100]
        entry  = e.get("entry_zone", "")
        stop   = e.get("hard_stop", "").split("—")[0].split("(")[0].strip()

        line = (
            f"{urgency}{icon} **{e['ticker']}** — {e['earnings_date']} ({date_label})\n"
            f"  *{thesis_snip}{'...' if len(e.get('thesis','')) > 100 else ''}*"
        )
        if entry:
            line += f"\n  📍 Entry: {entry}"
        if stop:
            line += f"  🛑 Stop: {stop}"
        lines.append(line)

    embed = discord.Embed(
        title=f"📅  Earnings Radar — {len(upcoming)} name(s) in next {days} days",
        description="\n\n".join(lines),
        color=COLOR_GOLD,
        timestamp=_ts(),
    )
    embed.add_field(
        name="Tip",
        value=(
            "Consider closing or reducing positions **before** earnings — "
            "implied vol collapses after the print.\n"
            "Use `!deepdive TICKER` for a full catalyst + risk breakdown."
        ),
        inline=False,
    )
    embed.set_footer(
        text="Conquest Earnings Radar  •  Dates sourced from Yahoo Finance  •  Always verify"
    )

    er_channel = await _get_channel("earnings-radar", "stocks", "general")
    dest = er_channel if er_channel else ctx.channel
    await dest.send(embed=embed)
    if dest != ctx.channel:
        await ctx.send(
            f"📅 Earnings radar ({len(upcoming)} names) posted to {dest.mention}",
            delete_after=8,
        )


# ── !testchannels ─────────────────────────────────────────────────────────────

@bot.command(name="testchannels", aliases=["tc", "techchannels"])
async def testchannels_cmd(ctx):
    """Fire a test message to every dedicated channel to verify routing."""

    CHANNEL_MAP = [
        ("morning-briefing", "🗞",  "Morning Intelligence Brief",  "Auto-posts the daily macro brief at 9:00 AM ET."),
        ("trade-alerts",     "🧪",  "Trade Alerts",                "New paper trades post here at 9:35 AM ET."),
        ("trade-log",        "📋",  "Trade Log",                   "Each individual stop/target/expiry close posts here."),
        ("evening-debrief",  "📊",  "Evening Debrief",             "Claude-narrated EOD wrap with paper trading summary at 4:05 PM ET."),
        ("daily-pnl",        "💰",  "Daily P&L",                   "Short P&L one-liner posts here at 4:05 PM ET."),
        ("stocks",           "📈",  "Stock Watchlist",             "Auto-scan results post here at market open (9:30 AM). !watch and !deepdive cards also post here."),
        ("earnings-radar",   "📅",  "Earnings Radar",              "Upcoming earnings for watchlist names auto-post each morning at 9:00 AM."),
        ("macro-worldview",  "🌍",  "Macro Worldview",             "FRED macro snapshot auto-posts here each morning at 9:00 AM alongside the brief."),
        ("live-positions",   "📈",  "Live Positions",              "Auto-updates at noon and 3:30 PM ET with all open paper positions."),
        ("screener",         "📊",  "Screener",                    "Pre-screener results (top candidates from 129-ticker scan) post here each morning at 9:35 AM ET. Weekly value/growth screen also posts here every Monday."),
        ("status-dashboard", "🏆",  "Status Dashboard",            "Running paper trading stats auto-post here every evening at 4:05 PM ET."),
        ("agent-brain",      "🤖",  "Agent Brain",                 "Agent weight updates and learning events post here automatically after trades close."),
        ("agent-debate",     "🗣",  "Agent Debate",                "Full bull/bear debate transcript posts here for every paper trade. See what the bull advocate argued, what the bear punched holes in, and the Portfolio Manager's final verdict."),
        ("missed-trades",    "🚫",  "Missed Trades",               "Trades the PM debate vetoed — shows what the system almost took and exactly why it said no. Learn from skipped setups."),
        ("sector-rotation",  "🔄",  "Sector Rotation",             "Daily sector ETF performance and rotation signal posts here each morning at 9:00 AM ET alongside the briefing."),
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
_auto_scan_dates       = set()  # dates market-open watchlist scan was auto-posted
_auto_discovery_dates  = set()  # dates auto-thesis discovery was run
_positions_posted      = set()  # (date, label) tuples — "noon" and "preclose" positions updates
_screener_dates        = set()  # Mondays the weekly screener was auto-posted


async def _get_channel(primary: str, *fallbacks: str):
    """
    Find a Discord text channel by name.
    Priority order:
      1. Saved channel ID for this specific channel (ch_morning_briefing, etc.)
      2. Exact name match across all guilds (primary, then each fallback)
      3. bot_alerts_channel_id — only used as last resort if ALL names fail

    Usage:
        ch = await _get_channel("morning-briefing", "general")
        ch = await _get_channel("trade-log", "trade-alerts")
    """
    s = _load_settings()

    # 1. Check settings for a saved channel ID specific to this channel only
    id_key = f"ch_{primary.replace('-', '_')}"  # e.g. "ch_morning_briefing"
    specific_id = s.get(id_key)
    if specific_id:
        ch = bot.get_channel(int(specific_id))
        if ch:
            return ch

    # 2. Search all guilds by exact channel name (primary, then fallbacks)
    names_to_try = [primary] + list(fallbacks)
    for name in names_to_try:
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if ch.name.lower() == name.lower():
                    return ch

    # 3. Last resort: generic bot_alerts_channel_id (only if no named channel found)
    generic_id = s.get("bot_alerts_channel_id")
    if generic_id:
        ch = bot.get_channel(int(generic_id))
        if ch:
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
    global _paper_generated_dates, _paper_notified_ids, _paper_eod_dates, \
           _auto_scan_dates, _auto_discovery_dates, _positions_posted

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
        ch_watchlist= await _get_channel("stocks",           "general")
        ch_positions= await _get_channel("live-positions",   "general")

        # ── 0a. Auto-scan at market open (9:30–10:15 AM, once per day) ─────────
        open_window = (h == 9 and m >= 30) or (h == 10 and m <= 15)
        if open_window and today not in _auto_scan_dates:
            _auto_scan_dates.add(today)
            s_cfg = _load_settings()
            wl_tickers = s_cfg.get("watchlist", "").split()
            if wl_tickers and ch_watchlist:
                try:
                    def _do_open_scan():
                        from alerts.scanner import scan_watchlist
                        return scan_watchlist(wl_tickers)

                    scan_res  = await _run_sync(_do_open_scan)
                    entries   = [r for r in scan_res if r.get("entry_signal")  and not r.get("error")]
                    crosses   = [r for r in scan_res if r.get("macd_cross_up") and not r.get("entry_signal") and not r.get("error")]

                    if entries or crosses:
                        color = COLOR_GREEN if entries else COLOR_ORANGE
                        title = (f"✦  {len(entries)} Entry Signal(s) at Open"
                                 if entries else
                                 f"↑  {len(crosses)} MACD Cross(es) at Open")
                        lines = []
                        for r in entries + crosses:
                            sig = "✦ **ENTRY**" if r.get("entry_signal") else "↑ MACD"
                            sqz = " 🔥SQZ" if r.get("sqz_fired") else (" ⚡SQZ" if r.get("sqz_on") else "")
                            chg = r.get("today_chg_pct", 0)
                            lines.append(
                                f"**{r['ticker']}** `${r['price']:.2f}` {chg:+.1f}%  "
                                f"MTF {r['mtf_score']}/3  RSI {r['rsi']:.0f}  ADX {r['adx']:.0f}  {sig}{sqz}"
                            )
                        embed = discord.Embed(
                            title=title,
                            description="\n".join(lines),
                            color=color,
                            timestamp=_ts(),
                        )
                        embed.set_footer(
                            text=f"Market Open Scan  •  {len(wl_tickers)} tickers  •  Conquest Trading"
                        )
                        await ch_watchlist.send(embed=embed)
                    else:
                        # Quiet open — post a brief "no signals" note so you know it ran
                        await ch_watchlist.send(
                            embed=discord.Embed(
                                title="✓  Market Open Scan — No Fresh Signals",
                                description=(
                                    f"Scanned {len(wl_tickers)} tickers at open. "
                                    "No entry signals or MACD crosses today. Wait for the setup."
                                ),
                                color=COLOR_DARK,
                                timestamp=_ts(),
                            )
                        )
                    print(f"[PaperLoop] Open scan done: {len(entries)} entries, {len(crosses)} MACD crosses")
                except Exception as e_scan:
                    print(f"[PaperLoop] Open scan error: {e_scan}")

        # ── 0b. Auto-discovery — scan universe, auto-thesis any new signal (10:00–10:45 AM) ──
        discovery_window = (h == 10 and m <= 45)
        if discovery_window and today not in _auto_discovery_dates:
            _auto_discovery_dates.add(today)
            if ch_watchlist:
                try:
                    from scan_universe import UNIVERSE, EXCLUDE_FROM_THESIS, \
                        MIN_MTF_SCORE, REQUIRE_ENTRY, MAX_ADDS_PER_DAY

                    def _scan_universe():
                        from alerts.scanner   import scan_watchlist
                        from watchlist_engine import get_entry
                        from scan_universe    import (
                            STOCK_MIN_PRICE, STOCK_MIN_ADX,
                            STOCK_MIN_MTF_SCORE, REQUIRE_ENTRY_SIGNAL,
                        )
                        results = scan_watchlist(UNIVERSE)
                        candidates = []
                        for r in results:
                            if r.get("error"):
                                continue
                            ticker = r.get("ticker", "")

                            # Skip ETFs / non-thesis tickers
                            if ticker in EXCLUDE_FROM_THESIS:
                                continue

                            # Skip if already on watchlist
                            if get_entry(ticker):
                                continue

                            # ── Stock-quality filters ──────────────────────
                            # Price floor — no sub-$15 stocks
                            if r.get("price", 0) < STOCK_MIN_PRICE:
                                continue

                            # Trend strength — ADX must confirm a real trend
                            if r.get("adx", 0) < STOCK_MIN_ADX:
                                continue

                            # Signal quality
                            has_signal = (
                                r.get("entry_signal") if REQUIRE_ENTRY_SIGNAL
                                else (r.get("entry_signal") or r.get("macd_cross_up"))
                            )
                            if not has_signal:
                                continue

                            # MTF score
                            if r.get("mtf_score", 0) < STOCK_MIN_MTF_SCORE:
                                continue

                            candidates.append(r)

                        # Best setups first: highest MTF, then RSI closest to 55
                        candidates.sort(
                            key=lambda x: (
                                -x.get("mtf_score", 0),
                                abs(x.get("rsi", 50) - 55),
                            )
                        )
                        return candidates[:MAX_ADDS_PER_DAY]

                    top_candidates = await _run_sync(_scan_universe)

                    if top_candidates:
                        await ch_watchlist.send(
                            embed=discord.Embed(
                                title=f"🔍  Auto-Discovery: {len(top_candidates)} New Signal(s) Found",
                                description=(
                                    "Running full AI analysis on each. Cards will follow shortly.\n"
                                    + "\n".join(
                                        f"• **{r['ticker']}** — MTF {r['mtf_score']}/3  "
                                        f"RSI {r['rsi']:.0f}  ADX {r['adx']:.0f}  "
                                        f"{'✦ ENTRY' if r.get('entry_signal') else '↑ MACD'}"
                                        for r in top_candidates
                                    )
                                ),
                                color=COLOR_PURPLE,
                                timestamp=_ts(),
                            )
                        )

                        # Analyze and post each candidate (sequential — ~20s each)
                        for r in top_candidates:
                            ticker = r["ticker"]
                            try:
                                def _analyze(t=ticker):
                                    from watchlist_engine import analyze_and_add
                                    return analyze_and_add(t)

                                entry = await _run_sync(_analyze)
                                embed = _build_watchlist_embed(entry)
                                # Tag as auto-discovered in footer
                                embed.set_footer(
                                    text="Auto-discovered by Conquest Scanner  •  Not financial advice"
                                )
                                await ch_watchlist.send(embed=embed)
                                print(f"[AutoDiscover] Added {ticker} to watchlist")
                            except Exception as e_t:
                                print(f"[AutoDiscover] Failed for {ticker}: {e_t}")
                                await ch_watchlist.send(
                                    f"⚠ Auto-analysis failed for **{ticker}**: {str(e_t)[:100]}"
                                )
                    else:
                        print(f"[AutoDiscover] No new signals above threshold today")

                except Exception as e_disc:
                    print(f"[PaperLoop] Auto-discovery error: {e_disc}")

        # ── 0c. Positions update — noon (12:00) and pre-close (3:30 PM) ────────
        for label, h_check, m_min, m_max in [
            ("noon",     12, 0,  14),
            ("preclose", 15, 30, 44),
        ]:
            pos_key = (today, label)
            if h == h_check and m_min <= m <= m_max and pos_key not in _positions_posted:
                _positions_posted.add(pos_key)
                if ch_positions:
                    try:
                        def _get_pos():
                            from paper_trader import load_trades, get_paper_stats
                            import yfinance as yf
                            open_t = [t for t in load_trades() if t.get("status") == "open"]
                            stats  = get_paper_stats()
                            total_cost = sum(abs(t.get("cost_basis", 0)) for t in open_t)
                            total_pnl  = sum(t.get("pnl", 0) for t in open_t)
                            return open_t, stats, total_cost, total_pnl

                        open_trades, stats, total_cost, total_pnl = await _run_sync(_get_pos)

                        if open_trades:
                            pct   = (total_pnl / total_cost * 100) if total_cost else 0
                            color = COLOR_GREEN if total_pnl >= 0 else COLOR_RED
                            label_display = "Midday" if label == "noon" else "Pre-Close"

                            lines = []
                            for t in sorted(open_trades, key=lambda x: x.get("pnl", 0), reverse=True):
                                pnl  = t.get("pnl", 0)
                                ppct = t.get("pnl_pct", 0) * 100
                                dot  = "🟢" if pnl >= 0 else "🔴"
                                tt   = t["trade_type"].replace("_", " ").title()
                                lines.append(
                                    f"{dot} **{t['ticker']}** ({tt}) | "
                                    f"{ppct:+.1f}% | day {t.get('days_held', 0)}"
                                )

                            pos_embed = discord.Embed(
                                title=(
                                    f"📈  {label_display} Positions  —  "
                                    f"${total_pnl:+.2f} ({pct:+.1f}%)"
                                ),
                                description="\n".join(lines),
                                color=color,
                                timestamp=_ts(),
                            )
                            pos_embed.add_field(
                                name="Summary",
                                value=(
                                    f"**{len(open_trades)}** open  •  "
                                    f"Cost basis ${total_cost:,.0f}  •  "
                                    f"All-time P&L **${stats.get('total_pnl', 0):+.2f}**  •  "
                                    f"Win rate {stats.get('win_rate', 0)*100:.1f}%"
                                ),
                                inline=False,
                            )
                            pos_embed.set_footer(
                                text=f"Conquest Trading  •  {label_display} Update  •  Paper simulation"
                            )
                            await ch_positions.send(embed=pos_embed)
                            print(f"[PaperLoop] {label_display} positions posted ({len(open_trades)} open)")
                    except Exception as e_pos:
                        print(f"[PaperLoop] Positions update error ({label}): {e_pos}")

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
                # Check if agent system was used (agent-generated trades have agent_consensus)
                agent_count = sum(1 for t in new_trades if t.get("agent_consensus"))
                if agent_count > 0:
                    avg_conf = sum(
                        t.get("agent_confidence", 0) for t in new_trades
                        if t.get("agent_consensus")
                    ) / agent_count
                    source_line = (
                        f"🤖 **6-Agent Swarm**: {agent_count} AI-selected trades  "
                        f"(avg confidence {avg_conf:.0%})\n"
                    )
                else:
                    source_line = "📡 Signal-based generation\n"

                embed = discord.Embed(
                    title=f"🧪  Paper Trades Generated  —  {today.strftime('%b %d')}",
                    description=(
                        f"{source_line}"
                        f"**{len(new_trades)} trades** placed across the universe.\n"
                        f"{breakdown}"
                    ),
                    color=COLOR_GOLD,
                    timestamp=_ts(),
                )
                embed.set_footer(
                    text="Auto-closes: options +50%/−75% · stocks +5%/−3% · max 5 days"
                )
                await ch_trades.send(embed=embed)

                # Post one card per trade with full reasoning
                for t in new_trades:
                    tt_label = t["trade_type"].replace("_", " ").title()
                    price    = t.get("entry_price") or t.get("entry_net_debit") or t.get("entry_net_credit", 0)
                    reasoning = t.get("reasoning", "")

                    trade_embed = discord.Embed(
                        title=f"🧪  {t['ticker']} — {tt_label}",
                        description=reasoning,
                        color=COLOR_PURPLE,
                        timestamp=_ts(),
                    )
                    # Strike / structure details
                    if t["trade_type"] in ("call_spread", "put_spread"):
                        trade_embed.add_field(
                            name="Structure",
                            value=(
                                f"${t.get('long_strike','?')} / ${t.get('short_strike','?')}  "
                                f"({'Calls' if t['trade_type']=='call_spread' else 'Puts'})\n"
                                f"Debit: **${price:.2f}**  |  Max profit: **${t.get('max_profit',0):.2f}**\n"
                                f"Breakeven: **${t.get('breakeven',0):.2f}**"
                            ),
                            inline=True,
                        )
                    elif t["trade_type"] == "iron_condor":
                        trade_embed.add_field(
                            name="Structure",
                            value=(
                                f"${t.get('long_put_k','?')} / ${t.get('short_put_k','?')} / "
                                f"${t.get('short_call_k','?')} / ${t.get('long_call_k','?')}\n"
                                f"Credit: **${price:.2f}**  |  Max loss: **${t.get('max_loss',0):.2f}**"
                            ),
                            inline=True,
                        )
                    elif t["trade_type"] in ("long_call", "long_put"):
                        trade_embed.add_field(
                            name="Structure",
                            value=(
                                f"Strike: **${t.get('strike','?')}**  |  Premium: **${price:.2f}**\n"
                                f"Breakeven: **${t.get('breakeven',0):.2f}**"
                            ),
                            inline=True,
                        )
                    else:
                        trade_embed.add_field(
                            name="Structure",
                            value=f"Entry: **${price:.2f}**  |  Shares: {t.get('shares','?')}",
                            inline=True,
                        )
                    # Signals field — show agent consensus if available
                    if t.get("agent_consensus"):
                        votes_str = "  ".join(
                            f"{k}: {v}" for k, v in
                            list(t.get("agent_votes", {}).items())[:3]
                        )
                        signals_text = (
                            f"🤖 {t['agent_consensus']}  {t.get('agent_confidence',0):.0%}  "
                            f"({t.get('agent_count',0)}/6 agree)\n"
                            f"{votes_str}"
                        )
                    else:
                        signals_text = (
                            f"MTF {t.get('mtf_score','?')}/3  ·  "
                            f"RSI {t.get('rsi_entry','?')}  ·  "
                            f"ADX {t.get('adx_entry', t.get('adx','?'))}"
                        )
                    trade_embed.add_field(
                        name="Signals",
                        value=signals_text,
                        inline=True,
                    )
                    # Debate round fields — show when PM ran the bull/bear review
                    if t.get("debate_pm"):
                        bull_bar = "█" * round(t.get("debate_bull_str", 0) * 10)
                        bear_bar = "█" * round(t.get("debate_bear_weak", 0) * 10)
                        debate_text = (
                            f"**Bull** `{bull_bar:<10}` {t.get('debate_bull_str',0):.0%}  "
                            f"**Bear** `{bear_bar:<10}` {t.get('debate_bear_weak',0):.0%}\n"
                            f"⚖️ PM: *{t.get('debate_pm','')}*"
                        )
                        trade_embed.add_field(
                            name="🗣 Bull/Bear Debate",
                            value=debate_text[:1000],
                            inline=False,
                        )
                    trade_embed.set_footer(text="Conquest Paper Trading  •  Simulation only")
                    await ch_trades.send(embed=trade_embed)

                    # ── Post full debate to #agent-debate (if debate ran) ──────
                    if t.get("debate_pm"):
                        ch_debate = await _get_channel("agent-debate")
                        if ch_debate:
                            try:
                                won_color   = COLOR_GREEN  # PM approved → proceed
                                bull_str    = t.get("debate_bull_str", 0)
                                bear_weak   = t.get("debate_bear_weak", 0)
                                bull_bar    = "█" * round(bull_str * 10) + "░" * (10 - round(bull_str * 10))
                                bear_bar    = "█" * round(bear_weak * 10) + "░" * (10 - round(bear_weak * 10))
                                tt_label    = t["trade_type"].replace("_", " ").title()
                                conf        = t.get("agent_confidence", 0)

                                debate_embed = discord.Embed(
                                    title=f"🗣  Agent Debate  —  {t['ticker']} {tt_label}",
                                    description=(
                                        f"6-agent swarm reached **{t.get('agent_count',0)}/6 consensus** "
                                        f"at **{conf:.0%}** confidence.\n"
                                        f"Bull/Bear debate ran before final trade approval."
                                    ),
                                    color=COLOR_PURPLE,
                                    timestamp=_ts(),
                                )
                                debate_embed.add_field(
                                    name=f"🐂  Bull Advocate  —  strength {bull_str:.0%}",
                                    value=f"`{bull_bar}` {bull_str:.0%}\n\n{t.get('debate_bull','')[:800]}",
                                    inline=False,
                                )
                                debate_embed.add_field(
                                    name=f"🐻  Bear Adversary  —  weakness found {bear_weak:.0%}",
                                    value=f"`{bear_bar}` {bear_weak:.0%}\n\n{t.get('debate_bear','')[:800]}",
                                    inline=False,
                                )
                                # PM verdict with visual signal strength
                                if bull_str >= bear_weak:
                                    verdict_icon = "✅"
                                    verdict_label = "PROCEED — Bull case stronger"
                                else:
                                    verdict_icon = "⚠️"
                                    verdict_label = "PROCEED — Despite bear concerns"
                                debate_embed.add_field(
                                    name=f"⚖️  Portfolio Manager Verdict  —  {verdict_icon} {verdict_label}",
                                    value=f"*\"{t.get('debate_pm','')}\"*",
                                    inline=False,
                                )
                                debate_embed.set_footer(
                                    text=(
                                        f"Conquest Agent Debate  •  "
                                        f"Inspired by TradingAgents + LLM-TradeBot  •  "
                                        f"Simulation only"
                                    )
                                )
                                await ch_debate.send(embed=debate_embed)
                            except Exception as _de_err:
                                print(f"[PaperLoop] Debate channel post failed: {_de_err}")

            # ── Post vetoed trades to #missed-trades ──────────────────────────
            ch_missed = await _get_channel("missed-trades")
            if ch_missed:
                try:
                    def _get_vetoed():
                        from conquest_agents import get_vetoed_trades
                        return get_vetoed_trades()
                    vetoed = await _run_sync(_get_vetoed)
                    if vetoed:
                        missed_header = discord.Embed(
                            title=f"🚫  Missed Trades  —  {today.strftime('%b %d')}  ({len(vetoed)} vetoed)",
                            description=(
                                "These tickers reached initial 4+/6 agent consensus but the "
                                "PM debate said **no**. Study these — sometimes the best trade is the one you skip."
                            ),
                            color=COLOR_ORANGE,
                            timestamp=_ts(),
                        )
                        await ch_missed.send(embed=missed_header)
                        for v in vetoed[:8]:   # cap at 8 per day
                            b_str = v.get("bull_strength", 0)
                            bw    = v.get("bear_weakness", 0)
                            bull_bar = "█" * round(b_str * 8) + "░" * (8 - round(b_str * 8))
                            bear_bar = "█" * round(bw * 8)    + "░" * (8 - round(bw * 8))
                            tt = v.get("suggested_type","").replace("_"," ").title()
                            ve = discord.Embed(
                                title=f"🚫  {v['ticker']}  {tt}  —  PM VETOED",
                                description=f"Initial consensus: **{v['agent_count']}/6 agents** at **{v['initial_conf']:.0%}** conf",
                                color=COLOR_RED,
                                timestamp=_ts(),
                            )
                            ve.add_field(
                                name=f"🐂 Bull Case  `{bull_bar}` {b_str:.0%}",
                                value=v.get("bull_case","")[:600],
                                inline=False,
                            )
                            ve.add_field(
                                name=f"🐻 Bear Case  `{bear_bar}` {bw:.0%}",
                                value=v.get("bear_case","")[:600],
                                inline=False,
                            )
                            ve.add_field(
                                name="⚖️ PM Decision",
                                value=f"*\"{v.get('pm_reasoning','')}\"*",
                                inline=False,
                            )
                            ve.set_footer(text="Conquest  •  Missed Trades  •  Studying skips is how you improve")
                            await ch_missed.send(embed=ve)
                except Exception as _mv_err:
                    print(f"[PaperLoop] Missed-trades post failed: {_mv_err}")

            # ── Post pre-screener summary to #screener ─────────────────────────
            ch_screener = await _get_channel("screener")
            if ch_screener:
                try:
                    def _get_screen():
                        from universe_screener import get_last_screen
                        return get_last_screen()
                    screen_results = await _run_sync(_get_screen)
                    if screen_results:
                        top10 = screen_results[:10]
                        lines = []
                        for i, r in enumerate(top10, 1):
                            sig = ""
                            if r.get("entry_signal"): sig = " ✦ **ENTRY**"
                            elif r.get("sqz_fired"):  sig = " 🔥 SQZ"
                            elif r.get("macd_cross_up"): sig = " ↑ MACD"
                            lines.append(
                                f"`{i:2}.` **{r['ticker']}** "
                                f"score={r.get('_score',0):.1f}  "
                                f"MTF {r.get('mtf_score',0)}/3  "
                                f"ADX {r.get('adx',0):.0f}{sig}"
                            )
                        scr_embed = discord.Embed(
                            title=f"📊  Pre-Screen Results  —  {today.strftime('%b %d')}",
                            description=(
                                f"Scanned **{len(screen_results)}** tickers → top **40** fed to agent swarm.\n\n"
                                + "\n".join(lines)
                            ),
                            color=COLOR_PURPLE,
                            timestamp=_ts(),
                        )
                        scr_embed.set_footer(
                            text="Conquest Universe Screener  •  129-ticker S&P 500 coverage"
                        )
                        await ch_screener.send(embed=scr_embed)
                except Exception as _se:
                    print(f"[PaperLoop] Screener post failed: {_se}")

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
            if t.get("reasoning"):
                embed.add_field(
                    name="Entry Thesis",
                    value=t["reasoning"][:1000],
                    inline=False,
                )
            if t.get("close_reasoning"):
                embed.add_field(
                    name="Why It Closed",
                    value=t["close_reasoning"][:1000],
                    inline=False,
                )
            embed.set_footer(
                text="Conquest Trading  •  paper simulation  •  not financial advice"
            )
            await ch_log.send(embed=embed)

            # ── Post agent weight update to #agent-brain ───────────────────────
            ch_brain = await _get_channel("agent-brain")
            if ch_brain:
                try:
                    def _get_weights():
                        from conquest_agents import get_agent_system
                        sys = get_agent_system()
                        return sys.weights.copy()
                    weights = await _run_sync(_get_weights)
                    won = pnl >= 0
                    w_lines = []
                    for ag, w in sorted(weights.items(), key=lambda x: -x[1]):
                        bar = "█" * int(w * 4)
                        w_lines.append(f"`{ag:<15}` {bar:<9} {w:.3f}")
                    brain_embed = discord.Embed(
                        title=f"🧠  Agent Weights Updated  —  {t['ticker']} {'WIN' if won else 'LOSS'}",
                        description="\n".join(w_lines),
                        color=COLOR_GREEN if won else COLOR_RED,
                        timestamp=_ts(),
                    )
                    brain_embed.set_footer(
                        text=f"Agents that called it right gain weight · wrong agents lose weight · learn rate ±0.08"
                    )
                    await ch_brain.send(embed=brain_embed)
                except Exception:
                    pass   # agent brain post is best-effort

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

                # Claude narrative debrief (always fires — covers closed + open positions)
                open_positions = stats.get("open_trades", [])
                if ch_eod and (today_closed or open_positions):
                    try:
                        def _get_narrative():
                            from conquest_brain import paper_evening_debrief
                            return paper_evening_debrief(
                                stats, today_closed, stats.get("total_pnl", 0)
                            )
                        narrative = await _run_sync(_get_narrative)
                        if narrative and "unavailable" not in narrative.lower():
                            narr_embed = discord.Embed(
                                title="⚔️  Conquest Intelligence — EOD Read",
                                description=narrative,
                                color=COLOR_PURPLE,
                                timestamp=_ts(),
                            )
                            # Add open positions count to footer so it's clear
                            footer_txt = (
                                f"{len(open_positions)} position(s) held overnight  •  "
                                "Conquest Intelligence Desk  •  Not financial advice"
                            )
                            narr_embed.set_footer(text=footer_txt)
                            await ch_eod.send(embed=narr_embed)
                    except Exception as e_narr:
                        print(f"[PaperLoop] Narrative debrief error: {e_narr}")

                # Gmail EOD digest
                try:
                    def _send_email():
                        from conquest_brain import send_eod_email
                        _narr = narrative if "narrative" in locals() else ""
                        return send_eod_email(stats, today_closed, _narr)
                    await _run_sync(_send_email)
                except Exception as e_email:
                    print(f"[PaperLoop] EOD email skipped: {e_email}")

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

                # Full stats → #status-dashboard
                ch_status = await _get_channel("status-dashboard", "general")
                if ch_status:
                    wr_str = f"{stats['win_rate']*100:.1f}%" if stats["closed_count"] else "—"
                    sh_str = str(stats["sharpe"]) if stats.get("sharpe") is not None else "—"
                    pf_str = str(stats["profit_factor"]) if stats.get("profit_factor") else "—"
                    status_embed = discord.Embed(
                        title=f"🏆  Daily Stats — {today.strftime('%b %d')}",
                        color=COLOR_GREEN if stats.get("total_pnl", 0) >= 0 else COLOR_RED,
                        timestamp=_ts(),
                    )
                    status_embed.add_field(
                        name="Performance",
                        value=(
                            f"**Win Rate:** {wr_str}\n"
                            f"**Sharpe:** {sh_str}  |  **Profit Factor:** {pf_str}\n"
                            f"**Total P&L:** ${stats.get('total_pnl', 0):+.2f}\n"
                            f"**Trades:** {stats['total_trades']} "
                            f"({stats['open_count']} open · {stats['closed_count']} closed)"
                        ),
                        inline=False,
                    )
                    if stats.get("by_type"):
                        type_lines = []
                        for tt, d in sorted(
                            stats["by_type"].items(),
                            key=lambda x: -x[1]["total_pnl"]
                        ):
                            icon = {
                                "call_spread": "📈", "put_spread": "📉",
                                "long_call": "🟢", "long_put": "🔴",
                                "iron_condor": "🦅", "stock_long": "💹",
                                "stock_short": "🔻",
                            }.get(tt, "•")
                            type_lines.append(
                                f"{icon} {tt.replace('_',' ')} · "
                                f"{d['count']}t · {d['win_rate']*100:.0f}% win · "
                                f"avg ${d['avg_pnl']:+.2f}"
                            )
                        status_embed.add_field(
                            name="By Type",
                            value="\n".join(type_lines),
                            inline=False,
                        )
                    status_embed.set_footer(
                        text="Conquest Trading  •  Paper Simulation  •  Not financial advice"
                    )
                    await ch_status.send(embed=status_embed)

    except Exception as e:
        print(f"[PaperLoop] Error: {e}")


@paper_trading_loop.before_loop
async def before_paper_loop():
    await bot.wait_until_ready()


# ── _post_screener_results — shared helper for weekly screener output ─────────

async def _post_screener_results(channel, screen_results: list, date_obj):
    """
    Post weekly screener results to a channel.
    Handles empty results, errors, and splits across multiple embeds
    if there are more than 15 tickers (Discord embed description limit).
    """
    if not screen_results:
        await channel.send(
            "⚠️ Screener returned no results. This usually means the AI call failed or "
            "data was unavailable for all tickers. Try `!runmorning` again."
        )
        return

    valid   = [r for r in screen_results if r.get("verdict") != "ERROR"]
    errors  = [r for r in screen_results if r.get("verdict") == "ERROR"]
    undervalued = [r for r in valid if r.get("verdict") == "UNDERVALUED"]

    date_str = date_obj.strftime("%b %d, %Y") if hasattr(date_obj, "strftime") else str(date_obj)

    # Send in batches of 15 so we never overflow the embed description
    CHUNK = 15
    for batch_idx, start in enumerate(range(0, len(valid), CHUNK)):
        chunk = valid[start : start + CHUNK]
        lines = []
        for r in chunk:
            verdict = r.get("verdict", "?")
            score   = r.get("score")
            icon    = "🟢" if verdict == "UNDERVALUED" else ("🟡" if verdict == "FAIRLY VALUED" else "🔴")
            score_s = f"{score:.2f}" if score is not None else "N/A"
            lines.append(
                f"{icon} **{r['ticker']}** — score {score_s}  _{verdict}_\n"
                f"  {r.get('reason','')[:120]}"
            )

        part_label = f"Part {batch_idx + 1}" if len(valid) > CHUNK else date_str
        embed = discord.Embed(
            title=f"📊  Weekly Value/Growth Screen — {part_label}",
            description="\n\n".join(lines),
            color=COLOR_GREEN if undervalued else COLOR_DARK,
            timestamp=_ts(),
        )
        if batch_idx == 0:
            embed.add_field(
                name=f"🟢 Undervalued picks ({len(undervalued)} names)",
                value=(", ".join(f"**{r['ticker']}**" for r in undervalued[:10]) or "None this week"),
                inline=False,
            )
            embed.add_field(
                name="How to read",
                value=(
                    "Score = P/S ÷ Revenue Growth %.  Lower = more growth per dollar.\n"
                    "Use `!watch TICKER` for a full AI thesis on any name."
                ),
                inline=False,
            )
        embed.set_footer(text=f"Conquest Weekly Screen  •  {len(valid)} tickers  •  Not financial advice")
        await channel.send(embed=embed)

    if errors:
        err_tickers = ", ".join(r["ticker"] for r in errors[:10])
        await channel.send(f"⚠️ Data unavailable for {len(errors)} tickers: {err_tickers}")


# ── _post_full_brief — shared helper for both auto and manual morning brief ───

async def _post_full_brief(channel, sections: dict, discord_summary: str,
                           snapshot: dict, sectors: list, title_suffix: str = ""):
    """
    Post the full Morning Intelligence Brief to a Discord channel.
    Sends:
      1. Header embed — market snapshot numbers + sector rotation + summary
      2. One text message per brief section (Macro, Overnight, Data vs Consensus,
         Sector Positioning, Portfolio Implications, What to Watch)
    """
    title = f"⚔️  Conquest Intelligence Brief" + (f"  —  {title_suffix}" if title_suffix else "")

    # ── Embed 1: market snapshot + sector rotation + top-line summary ─────────
    description = discord_summary or (sections.get("macro_regime", "")[:500] + "…") or \
                  "Morning brief generated — full analysis below."

    embed = discord.Embed(
        title=title,
        description=description,
        color=COLOR_PURPLE,
        timestamp=_ts(),
    )

    mkt_lines = []
    for key, label in [("SPY","SPY"),("QQQ","QQQ"),("DIA","DIA"),
                        ("^VIX","VIX"),("^TNX","10Y Yld"),("^IRX","2Y Yld"),
                        ("UUP","Dollar"),("GLD","Gold"),("CL=F","Oil"),("HYG","HYG")]:
        s_data = snapshot.get(key, {})
        if s_data:
            sign = "▲" if s_data["chg"] > 0 else ("▼" if s_data["chg"] < 0 else "–")
            mkt_lines.append(f"{sign} **{label}** {s_data['price']} ({s_data['chg']:+.2f}%)")
    if mkt_lines:
        mid = len(mkt_lines) // 2
        embed.add_field(
            name="📊 Market Snapshot",
            value="  ".join(mkt_lines[:mid]) + "\n" + "  ".join(mkt_lines[mid:]),
            inline=False,
        )

    if sectors:
        top3 = "  ".join(
            f"{'▲' if d['ret5'] > 0 else '▼'} **{d['name']}** {d['ret5']:+.1f}%"
            for d in sectors[:3]
        )
        bot3 = "  ".join(
            f"▼ **{d['name']}** {d['ret5']:+.1f}%"
            for d in sectors[-3:]
        )
        embed.add_field(name="🟢 Leading Sectors (5d)", value=top3, inline=True)
        embed.add_field(name="🔴 Lagging Sectors (5d)", value=bot3, inline=True)

    embed.set_footer(text="Conquest Intelligence Desk  •  Not financial advice")
    await channel.send(embed=embed)

    # ── Messages 2–7: one per section, full prose ─────────────────────────────
    SECTION_HEADERS = [
        ("macro_regime",          "**MACRO REGIME ASSESSMENT**"),
        ("overnight",             "**OVERNIGHT & PRE-MARKET DEVELOPMENTS**"),
        ("data_vs_consensus",     "**WHAT THE DATA IS TELLING YOU VS. WHAT CONSENSUS BELIEVES**"),
        ("sector_positioning",    "**SECTOR POSITIONING RATIONALE**"),
        ("portfolio_implications","**PORTFOLIO IMPLICATIONS**"),
        ("what_to_watch",         "**WHAT TO WATCH TODAY**"),
    ]

    for key, header in SECTION_HEADERS:
        text = sections.get(key, "").strip()
        if not text:
            continue
        # Discord message limit is 2000 chars; split if needed
        full = f"{header}\n\n{text}"
        # Send in chunks of 1900 chars to stay under limit
        while full:
            chunk = full[:1900]
            # Try to break at a sentence boundary
            if len(full) > 1900:
                last_period = chunk.rfind(". ")
                if last_period > 1200:
                    chunk = full[:last_period + 1]
            await channel.send(chunk)
            full = full[len(chunk):].lstrip()

    await channel.send(
        "─────────────────────────────────────\n"
        "Full brief with charts at `/brief` on the web app."
    )


# ── Auto morning briefing ─────────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def morning_briefing_task():
    """Fires every minute, posts briefing once at 9 AM ET on weekdays."""
    global _briefing_sent_date, _screener_dates
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

        await _post_full_brief(channel, sections, discord_summary, snapshot, sectors, title_suffix="Auto 9 AM ET")
        print(f"[Bot] Auto morning brief posted at {now_et.strftime('%Y-%m-%d %H:%M ET')}")

        # ── Earnings radar — post to #earnings-radar if any watchlist names report this week ──
        try:
            def _check_earnings():
                from watchlist_engine import get_upcoming_earnings
                return get_upcoming_earnings(7)   # next 7 days only for the auto morning post

            upcoming_earnings = await _run_sync(_check_earnings)
            if upcoming_earnings:
                er_channel = await _get_channel("earnings-radar", "morning-briefing", "general")
                if er_channel:
                    lines = []
                    for e in upcoming_earnings:
                        d       = e["days_to"]
                        icon    = _conviction_icon(e.get("conviction", "MEDIUM"))
                        urgency = "🔴 " if d <= 2 else ("🟡 " if d <= 4 else "🟢 ")
                        label   = "TODAY" if d == 0 else ("TOMORROW" if d == 1 else f"in {d}d ({e['earnings_date']})")
                        lines.append(
                            f"{urgency}{icon} **{e['ticker']}** — {label}\n"
                            f"  *{e.get('thesis','')[:100]}{'...' if len(e.get('thesis','')) > 100 else ''}*"
                        )
                    er_embed = discord.Embed(
                        title=f"📅  Earnings This Week — {len(upcoming_earnings)} watchlist name(s)",
                        description="\n\n".join(lines),
                        color=COLOR_GOLD,
                        timestamp=_ts(),
                    )
                    er_embed.add_field(
                        name="Reminder",
                        value="Consider reducing size before the print. IV crush hits hard after earnings.",
                        inline=False,
                    )
                    er_embed.set_footer(text="Conquest Earnings Radar  •  Verify dates before trading")
                    await er_channel.send(embed=er_embed)
                    print(f"[Bot] Earnings radar posted: {len(upcoming_earnings)} name(s)")
        except Exception as e_er:
            print(f"[Bot] Earnings radar error: {e_er}")

        # ── Macro worldview — post FRED snapshot to #macro-worldview ──────────
        try:
            macro_ch = await _get_channel("macro-worldview", "general")
            if macro_ch:
                await _post_macro_embed(macro_ch)
                print(f"[Bot] Macro snapshot posted to #{macro_ch.name}")
        except Exception as e_mac:
            print(f"[Bot] Auto-macro error: {e_mac}")

        # ── Sector rotation — ETF heat map to #sector-rotation ───────────────
        try:
            sector_ch = await _get_channel("sector-rotation", "macro-worldview", "general")
            if sector_ch and sectors:
                lines = []
                for s_item in sectors:
                    name   = s_item.get("sector", s_item.get("name","?"))
                    chg    = s_item.get("change_pct", s_item.get("chg_pct", 0)) or 0
                    signal = s_item.get("signal", s_item.get("trend",""))
                    icon   = "🟢" if chg > 1 else ("🔴" if chg < -1 else "⚪")
                    lines.append(f"{icon} **{name}** {chg:+.1f}%  {signal}")
                if not lines:
                    lines = ["Sector data unavailable — check macro dashboard manually."]
                sec_embed = discord.Embed(
                    title=f"🔄  Sector Rotation  —  {today.strftime('%b %d')}",
                    description="\n".join(lines[:12]),
                    color=COLOR_PURPLE,
                    timestamp=_ts(),
                )
                sec_embed.set_footer(text="Conquest  •  Sector ETF heat map  •  Not financial advice")
                await sector_ch.send(embed=sec_embed)
                print(f"[Bot] Sector rotation posted to #{sector_ch.name}")
        except Exception as e_sec:
            print(f"[Bot] Sector rotation error: {e_sec}")

        # ── Weekly screener — every Monday, screen universe for undervalued ────
        is_monday = (now_et.weekday() == 0)
        if is_monday and today not in _screener_dates:
            _screener_dates.add(today)
            try:
                screener_ch = await _get_channel("screener", "general")
                if screener_ch:
                    await screener_ch.send("📊 Running weekly value/growth screen across the universe...")

                    def _run_weekly_screen():
                        from scan_universe       import UNIVERSE, EXCLUDE_FROM_THESIS
                        from watchlist_engine    import fetch_ticker_data, build_data_block
                        from conquest_brain      import watchlist_screener
                        # Screen stocks only — no ETFs
                        tickers = [t for t in UNIVERSE if t not in EXCLUDE_FROM_THESIS][:40]
                        pairs   = []
                        for t in tickers:
                            try:
                                d = fetch_ticker_data(t)
                                b = build_data_block(d)
                                pairs.append((t, b))
                            except Exception:
                                pass
                        return watchlist_screener(pairs)

                    screen_results = await _run_sync(_run_weekly_screen)
                    await _post_screener_results(screener_ch, screen_results, today)
                    print(f"[Bot] Weekly screener posted: {len(screen_results)} tickers")
            except Exception as e_sc:
                await screener_ch.send(f"⚠️ Screener error: {e_sc}")
                print(f"[Bot] Weekly screener error: {e_sc}")

    except Exception as e:
        print(f"[Bot] Auto-briefing task error: {e}")


@morning_briefing_task.before_loop
async def before_morning_briefing():
    await bot.wait_until_ready()


# ── !runmorning — manually fire the 9 AM bundle for testing ──────────────────

@bot.command(name="runmorning", aliases=["rm", "testmorning", "forcemorning"])
async def runmorning_cmd(ctx):
    """Manually fire all 9 AM auto-posts: brief → earnings radar → macro → screener (if Monday)."""
    import pytz
    global _briefing_sent_date, _screener_dates

    await ctx.send("⚔️ Firing all 9 AM auto-posts now — check each channel...", delete_after=10)

    s = _load_settings()

    # ── 1. Morning brief ───────────────────────────────────────────────────────
    try:
        channel = await _get_channel("morning-briefing", "general")
        if not channel:
            await ctx.send("❌ Can't find `#morning-briefing` channel.", delete_after=15)
        else:
            watchlist = s.get("watchlist", "").split()
            await channel.send("⚔️ Generating morning intelligence brief...")

            def _do_brief():
                from morning_brief import generate_brief
                return generate_brief(watchlist=watchlist)

            brief = await _run_sync(_do_brief)
            sections        = brief.get("sections", {})
            discord_summary = brief.get("discord_summary", "")
            snapshot        = brief.get("snapshot", {})
            sectors         = brief.get("sector_rotation", [])

            await _post_full_brief(channel, sections, discord_summary, snapshot, sectors, title_suffix="Manual Run")
            _briefing_sent_date = datetime.now(pytz.timezone("America/New_York")).date()
            print("[Bot] !runmorning — morning brief posted")
    except Exception as e:
        await ctx.send(f"❌ Morning brief error: {e}", delete_after=20)
        print(f"[Bot] !runmorning brief error: {e}")

    # ── 2. Earnings radar ──────────────────────────────────────────────────────
    try:
        def _check_earnings():
            from watchlist_engine import get_upcoming_earnings
            return get_upcoming_earnings(7)

        upcoming_earnings = await _run_sync(_check_earnings)
        er_channel = await _get_channel("earnings-radar", "morning-briefing", "general")
        if er_channel and upcoming_earnings:
            lines = []
            for e in upcoming_earnings:
                d       = e["days_to"]
                icon    = _conviction_icon(e.get("conviction", "MEDIUM"))
                urgency = "🔴 " if d <= 2 else ("🟡 " if d <= 4 else "🟢 ")
                label   = "TODAY" if d == 0 else ("TOMORROW" if d == 1 else f"in {d}d ({e['earnings_date']})")
                lines.append(
                    f"{urgency}{icon} **{e['ticker']}** — {label}\n"
                    f"  *{e.get('thesis','')[:100]}{'...' if len(e.get('thesis','')) > 100 else ''}*"
                )
            er_embed = discord.Embed(
                title=f"📅  Earnings This Week — {len(upcoming_earnings)} watchlist name(s)",
                description="\n\n".join(lines),
                color=COLOR_GOLD,
                timestamp=_ts(),
            )
            er_embed.add_field(
                name="Reminder",
                value="Consider reducing size before the print. IV crush hits hard after earnings.",
                inline=False,
            )
            er_embed.set_footer(text="Conquest Earnings Radar  •  Verify dates before trading")
            await er_channel.send(embed=er_embed)
            print(f"[Bot] !runmorning — earnings radar posted: {len(upcoming_earnings)} name(s)")
        elif er_channel and not upcoming_earnings:
            await er_channel.send("📅 No earnings for watchlist names in the next 7 days.")
            print("[Bot] !runmorning — earnings radar: no upcoming earnings")
    except Exception as e:
        print(f"[Bot] !runmorning earnings error: {e}")

    # ── 3. Macro worldview ────────────────────────────────────────────────────
    try:
        macro_ch = await _get_channel("macro-worldview", "general")
        if macro_ch:
            await _post_macro_embed(macro_ch)
            print(f"[Bot] !runmorning — macro posted to #{macro_ch.name}")
    except Exception as e:
        print(f"[Bot] !runmorning macro error: {e}")

    # ── 4. Weekly screener (run regardless of day when triggered manually) ────
    try:
        screener_ch = await _get_channel("screener", "general")
        if screener_ch:
            await screener_ch.send("📊 Running weekly value/growth screen across the universe — this takes ~60 seconds...")

            def _run_weekly_screen():
                from scan_universe    import UNIVERSE, EXCLUDE_FROM_THESIS
                from watchlist_engine import fetch_ticker_data, build_data_block
                from conquest_brain   import watchlist_screener
                tickers = [t for t in UNIVERSE if t not in EXCLUDE_FROM_THESIS][:40]
                pairs = []
                for t in tickers:
                    try:
                        d = fetch_ticker_data(t)
                        b = build_data_block(d)
                        pairs.append((t, b))
                    except Exception:
                        pass
                return watchlist_screener(pairs)

            import datetime as _dt
            screen_results = await _run_sync(_run_weekly_screen)
            await _post_screener_results(screener_ch, screen_results, _dt.date.today())
            print(f"[Bot] !runmorning — screener posted: {len(screen_results)} tickers")
    except Exception as e:
        await screener_ch.send(f"⚠️ Screener error: {e}")
        print(f"[Bot] !runmorning screener error: {e}")

    await ctx.send("✅ All 9 AM posts fired. Check `#morning-briefing`, `#earnings-radar`, `#macro-worldview`, `#screener`.", delete_after=30)


# ── AI Chatbot ────────────────────────────────────────────────────────────────
# Four ways to trigger (all work in ANY channel):
#   1. ?your question       — fastest, works everywhere, no commands needed
#   2. !ask your question   — explicit command form
#   3. @mention the bot     — mention anywhere
#   4. post in #conquest-ai — freeform chat channel

_AI_SYSTEM_PROMPT = """You are Conquest, the AI intelligence engine embedded in this Discord server.
You are talking to Max — a Temple University student learning quantitative trading.

About this system:
- Automated paper trading bot: 10 trades/day from a 6-agent Claude Haiku swarm
- Agents: market_scanner, valuation, technicals, catalysts, risk, options_flow
- Agents learn over time — weights shift based on win/loss outcomes
- Trades: stocks long/short, call spreads, put spreads, long calls, long puts, iron condors
- Full live data pipeline: FRED macro, yfinance, Alpaca, Finnhub, PostgreSQL, Notion trade journal
- Morning brief 9 AM ET · Paper trades 9:35 AM · EOD wrap 4:05 PM

IMPORTANT — what you CAN do:
- You receive a live snapshot of current paper trades and stats injected into every message
- Use that data to answer specific questions about open positions, P&L, trade types
- Reference actual tickers, actual dollar amounts, actual days held from the context
- If the user asks about their trades, the data IS there — use it, don't say you can't access it

Your role: be the senior analyst on the desk. Direct, specific, data-anchored.
Keep Discord replies under 400 words. Plain prose, not heavy bullet lists.
Think Michael Burry briefing a junior trader — not a customer service bot."""

# All channels where free-typing gets an AI response (no prefix needed).
# Conquest-specific channels are included so you can ask questions anywhere
# in your server without switching to a dedicated AI channel.
_AI_CHAT_CHANNELS = {
    # ── dedicated AI channels ──────────────────────────────────────────────
    "conquest-ai", "ask-conquest", "ai-chat",
    # ── active discussion / general ───────────────────────────────────────
    "general", "stocks", "trading", "options", "charts",
    "trade-alerts", "trade-log", "options-watchlist",
    # ── agent system ──────────────────────────────────────────────────────
    "agent-debate", "agent-brain",
    # ── research & intelligence ───────────────────────────────────────────
    "watchlist", "earnings-radar", "screener",
    "morning-briefing", "macro-worldview",
    "congressional-tracker", "economic-calendar",
    "sector-rotation", "missed-trades",
    # ── performance ───────────────────────────────────────────────────────
    "evening-debrief", "daily-pnl", "status-dashboard", "live-positions",
}


async def _conquest_ai_reply(message: discord.Message, question: str):
    """Core AI response function. Calls Claude, handles chunking, posts reply."""
    if not question.strip():
        return

    # Inject a rich live snapshot of paper trading state into every AI reply
    context_note = ""
    try:
        def _get_ctx():
            from paper_trader import get_paper_stats
            s = get_paper_stats()
            open_t  = s.get("open_trades", [])
            # Build per-position lines
            pos_lines = []
            for t in open_t:
                pnl     = t.get("pnl", 0) or 0
                pnl_pct = (t.get("pnl_pct", 0) or 0) * 100
                days    = t.get("days_held", 0) or 0
                pos_lines.append(
                    f"  {t.get('ticker','?')} {t.get('trade_type','?').replace('_',' ')} "
                    f"P&L ${pnl:+.2f} ({pnl_pct:+.1f}%) day {days}/5"
                )
            pos_block = "\n".join(pos_lines) if pos_lines else "  none"
            return (
                f"[LIVE CONQUEST DATA]\n"
                f"Paper trades: {s['total_trades']} total | {s['open_count']} open | "
                f"{s.get('closed_count',0)} closed\n"
                f"Win rate: {s.get('win_rate',0)*100:.1f}% | "
                f"Total P&L: ${s.get('total_pnl',0):+.2f} | "
                f"Sharpe: {s.get('sharpe') or 'N/A'}\n"
                f"Open positions:\n{pos_block}"
            )
        context_note = await _run_sync(_get_ctx)
    except Exception:
        pass

    async with message.channel.typing():
        def _call_ai():
            from conquest_brain import _get_client
            full_question = f"{context_note}\n\nUser question: {question}" if context_note else question
            msg = _get_client().messages.create(
                model     = "claude-haiku-4-5",
                max_tokens= 600,
                system    = _AI_SYSTEM_PROMPT,
                messages  = [{"role": "user", "content": full_question}],
            )
            return msg.content[0].text.strip()

        try:
            answer = await _run_sync(_call_ai)
        except Exception as e:
            answer = f"⚠ AI unavailable right now: {str(e)[:100]}"

    # Discord message limit is 2000 chars — split cleanly if needed
    while answer:
        chunk = answer[:1900]
        if len(answer) > 1900:
            # Break at last sentence boundary
            last_period = chunk.rfind(". ")
            if last_period > 800:
                chunk = answer[:last_period + 1]
        await message.channel.send(chunk)
        answer = answer[len(chunk):].lstrip()


@bot.command(name="ask", aliases=["q", "question", "chat", "ai"])
async def ask_cmd(ctx, *, question: str = ""):
    """Ask Conquest AI anything about trading, options, or the system."""
    if not question:
        await ctx.send("Usage: `!ask <your question>`  e.g. `!ask what is an iron condor?`")
        return
    await _conquest_ai_reply(ctx.message, question)


@bot.event
async def on_message(message: discord.Message):
    """
    AI responds in any channel via four triggers:
      1. ?question       — leading ? anywhere (fastest, no commands)
      2. !ask question   — explicit command (handled by bot.process_commands)
      3. @mention        — mention the bot anywhere
      4. #conquest-ai    — freeform AI chat channel
    Commands (!...) are always processed first — nothing ever breaks.
    """
    # Never respond to ourselves
    if message.author == bot.user:
        return

    # Always process commands first so !ask, !scan, etc. still work
    await bot.process_commands(message)

    content = message.content.strip()

    # Don't double-handle command messages
    if content.startswith("!"):
        return

    # ── Trigger 1: ? prefix — works in ANY channel, fastest shorthand ─────────
    # e.g.  ?what is a bull call spread
    #        ? why is NVDA down today
    if content.startswith("?") and len(content) > 2:
        question = content[1:].strip()
        if question:
            await _conquest_ai_reply(message, question)
        return

    # ── Trigger 2: @mention — works in any channel ────────────────────────────
    if bot.user in message.mentions:
        question = (content
                    .replace(f"<@{bot.user.id}>", "")
                    .replace(f"<@!{bot.user.id}>", "")
                    .strip())
        if question:
            await _conquest_ai_reply(message, question)
        else:
            await message.channel.send(
                "Hey! Ask me anything. Try `?what is delta` or `?why is NVDA down` "
                "from any channel — no need to type !ask."
            )
        return

    # ── Trigger 3: dedicated AI channel — freeform, no prefix needed ──────────
    if message.channel.name.lower() in _AI_CHAT_CHANNELS:
        await _conquest_ai_reply(message, content)


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
