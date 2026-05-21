# -*- coding: utf-8 -*-
"""
Quant Trading Web App
=====================
Run this file to launch the dashboard in your browser.
  python web_app.py
Then open: http://localhost:5000
"""
import sys
import os
import io
import contextlib

# Always resolve imports relative to this file, regardless of cwd
APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

import json
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)

SETTINGS_FILE = os.path.join(APP_DIR, "alerts_settings.json")
DEFAULT_WATCHLIST = "AAPL NVDA MSFT GOOGL AMZN META TSLA JPM XOM WMT COP SPY QQQ"


def _load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"webhook_url": "", "watchlist": DEFAULT_WATCHLIST, "auto_briefing": False}


def _save_settings(data: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def capture(func, *args, **kwargs):
    """Run a function and capture everything it prints to stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            func(*args, **kwargs)
        except SystemExit:
            pass
        except Exception as e:
            print(f"\n  ERROR: {e}")
    return buf.getvalue()


# ── Home ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── Macro Dashboard ───────────────────────────────────────────────────────────

@app.route("/macro", methods=["GET", "POST"])
def macro():
    output = ""
    tickers = []
    if request.method == "POST":
        raw = request.form.get("tickers", "").strip().upper()
        tickers = raw.split() if raw else []
        from macro.fetcher import fetch_macro_data, macro_health_score, sector_rotation_phase, stock_macro_warnings
        from datetime import date

        def run():
            from macro.dashboard import run as macro_run
            macro_run(tickers if tickers else None)

        output = capture(run)
    return render_template("tool.html",
                           title="Macro Dashboard",
                           output=output,
                           fields=[
                               {"name": "tickers", "label": "Tickers for stock warnings (optional)",
                                "placeholder": "e.g. XOM COP EOG", "value": " ".join(tickers)},
                           ])


# ── MTF Dashboard ─────────────────────────────────────────────────────────────

@app.route("/mtf", methods=["GET", "POST"])
def mtf():
    output = ""
    tickers_str = ""
    if request.method == "POST":
        tickers_str = request.form.get("tickers", "").strip().upper()

        def run():
            # MTF dashboard reads sys.argv — patch it temporarily
            import mtf_dashboard  # noqa: F401 — importing runs the module

        # MTF dashboard is a script not a function, so we run it via subprocess
        import subprocess, sys as _sys
        tickers_list = tickers_str.split() if tickers_str else []
        script = os.path.join(APP_DIR, "mtf_dashboard.py")
        cmd = [_sys.executable, script] + tickers_list
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=APP_DIR)
        output = result.stdout + (result.stderr if result.stderr else "")

    return render_template("tool.html",
                           title="Multi-Timeframe Dashboard",
                           output=output,
                           fields=[
                               {"name": "tickers", "label": "Tickers (space-separated, or leave blank for default)",
                                "placeholder": "e.g. AAPL NVDA XOM GOOGL WMT", "value": tickers_str},
                           ])


# ── Spread Finder ─────────────────────────────────────────────────────────────

@app.route("/spreads", methods=["GET", "POST"])
def spreads():
    output = ""
    form_data = {}
    if request.method == "POST":
        ticker = request.form.get("ticker", "").strip().upper()
        budget = request.form.get("budget", "1000").strip()
        dte    = request.form.get("dte", "37").strip()
        form_data = {"ticker": ticker, "budget": budget, "dte": dte}

        from spread_finder import find_spreads
        output = capture(find_spreads, ticker, float(budget), int(dte))

    return render_template("tool.html",
                           title="Spread Finder",
                           output=output,
                           fields=[
                               {"name": "ticker", "label": "Ticker", "placeholder": "e.g. AAPL",
                                "value": form_data.get("ticker", "")},
                               {"name": "budget", "label": "Max budget / risk ($)",
                                "placeholder": "1000", "value": form_data.get("budget", "1000")},
                               {"name": "dte", "label": "Target DTE (days to expiry)",
                                "placeholder": "37", "value": form_data.get("dte", "37")},
                           ])


# ── CSP Finder ────────────────────────────────────────────────────────────────

@app.route("/csp", methods=["GET", "POST"])
def csp():
    output = ""
    form_data = {}
    if request.method == "POST":
        ticker     = request.form.get("ticker", "").strip().upper()
        collateral = request.form.get("collateral", "15000").strip()
        dte        = request.form.get("dte", "30").strip()
        form_data  = {"ticker": ticker, "collateral": collateral, "dte": dte}

        from csp_finder import find_csps
        output = capture(find_csps, ticker, float(collateral), int(dte))

    return render_template("tool.html",
                           title="Cash-Secured Put Finder",
                           output=output,
                           fields=[
                               {"name": "ticker", "label": "Ticker", "placeholder": "e.g. COP",
                                "value": form_data.get("ticker", "")},
                               {"name": "collateral", "label": "Max collateral ($)",
                                "placeholder": "15000", "value": form_data.get("collateral", "15000")},
                               {"name": "dte", "label": "Target DTE",
                                "placeholder": "30", "value": form_data.get("dte", "30")},
                           ])


# ── Covered Call ──────────────────────────────────────────────────────────────

@app.route("/covered-call", methods=["GET", "POST"])
def covered_call():
    output = ""
    form_data = {}
    if request.method == "POST":
        ticker      = request.form.get("ticker", "").strip().upper()
        entry_price = request.form.get("entry_price", "0").strip()
        shares      = request.form.get("shares", "100").strip()
        dte         = request.form.get("dte", "30").strip()
        form_data   = {"ticker": ticker, "entry_price": entry_price, "shares": shares, "dte": dte}

        from covered_call import analyze_covered_calls
        output = capture(analyze_covered_calls, ticker, float(entry_price), int(shares), int(dte))

    return render_template("tool.html",
                           title="Covered Call Analyzer",
                           output=output,
                           fields=[
                               {"name": "ticker", "label": "Ticker", "placeholder": "e.g. COP",
                                "value": form_data.get("ticker", "")},
                               {"name": "entry_price", "label": "Your entry price ($)",
                                "placeholder": "125.00", "value": form_data.get("entry_price", "")},
                               {"name": "shares", "label": "Shares owned",
                                "placeholder": "100", "value": form_data.get("shares", "100")},
                               {"name": "dte", "label": "Target DTE",
                                "placeholder": "30", "value": form_data.get("dte", "30")},
                           ])


# ── Iron Condor ───────────────────────────────────────────────────────────────

@app.route("/iron-condor", methods=["GET", "POST"])
def iron_condor():
    output = ""
    form_data = {}
    if request.method == "POST":
        ticker    = request.form.get("ticker", "").strip().upper()
        budget    = request.form.get("budget", "500").strip()
        dte       = request.form.get("dte", "90").strip()
        form_data = {"ticker": ticker, "budget": budget, "dte": dte}

        from iron_condor import find_condors
        output = capture(find_condors, ticker, float(budget), int(dte))

    return render_template("tool.html",
                           title="Iron Condor Finder",
                           output=output,
                           fields=[
                               {"name": "ticker", "label": "Ticker", "placeholder": "e.g. KO",
                                "value": form_data.get("ticker", "")},
                               {"name": "budget", "label": "Max budget per side ($)",
                                "placeholder": "500", "value": form_data.get("budget", "500")},
                               {"name": "dte", "label": "Target DTE",
                                "placeholder": "90", "value": form_data.get("dte", "90")},
                           ])


# ── Chart ─────────────────────────────────────────────────────────────────────

@app.route("/chart", methods=["GET", "POST"])
def chart():
    chart_html = None
    stats      = {}
    ticker     = ""
    error      = None
    if request.method == "POST":
        ticker = request.form.get("ticker", "").strip().upper()
        try:
            from chart_generator import generate_chart
            chart_html, stats = generate_chart(ticker)
        except Exception as e:
            error = str(e)
    return render_template("chart.html", ticker=ticker, chart_html=chart_html,
                           stats=stats, error=error)


# ── Alerts ────────────────────────────────────────────────────────────────────

@app.route("/alerts", methods=["GET", "POST"])
def alerts():
    settings = _load_settings()
    scan_results = None
    send_status  = None
    action       = request.form.get("action", "") if request.method == "POST" else ""

    if request.method == "POST":
        # Always read and save the form values
        webhook_url      = request.form.get("webhook_url", "").strip()
        watchlist        = request.form.get("watchlist", "").strip().upper()
        positions_webhook = request.form.get("positions_webhook", settings.get("positions_webhook","")).strip()
        settings = {"webhook_url": webhook_url, "watchlist": watchlist,
                    "positions_webhook": positions_webhook}
        _save_settings(settings)

        # Save auto_briefing toggle
        auto_briefing = request.form.get("auto_briefing") == "on"
        settings["auto_briefing"] = auto_briefing
        _save_settings(settings)

        if action == "test":
            from alerts.discord import test_webhook
            ok, send_status = test_webhook(webhook_url)

        elif action == "scan":
            from alerts.scanner import scan_watchlist
            tickers     = watchlist.split()
            scan_results = scan_watchlist(tickers)

        elif action == "send":
            from alerts.scanner import scan_watchlist
            from alerts.discord import send_discord_alert
            tickers     = watchlist.split()
            scan_results = scan_watchlist(tickers)
            ok, send_status = send_discord_alert(webhook_url, scan_results)

        elif action == "briefing":
            from alerts.scanner import scan_watchlist
            from conquest_brain import morning_briefing
            from datetime import datetime, timezone
            tickers       = watchlist.split()
            scan_results  = scan_watchlist(tickers)
            # Enrich with FRED macro context if available
            macro_notes = ""
            try:
                from macro.fred_data import fetch_fred_macro, fred_macro_context
                macro_notes = fred_macro_context(fetch_fred_macro())
            except Exception:
                pass
            briefing_text = morning_briefing(scan_results, macro_notes=macro_notes)
            # Send briefing to Discord
            if webhook_url and webhook_url.startswith("https://discord.com/api/webhooks/"):
                import requests as _req
                embed = {
                    "title": "⚔️  Conquest Morning Briefing",
                    "description": briefing_text,
                    "color": 0x7c6af7,
                    "footer": {"text": "Conquest Intelligence Desk"},
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                resp = _req.post(webhook_url, json={"embeds": [embed]}, timeout=10)
                ok = resp.status_code == 204
                send_status = "Morning briefing sent to Discord!" if ok else f"Error: {resp.status_code}"
            else:
                send_status = "Add your webhook URL to send the briefing to Discord."

    return render_template(
        "alerts.html",
        settings=settings,
        scan_results=scan_results,
        send_status=send_status,
        action=action,
        auto_briefing=settings.get("auto_briefing", False),
    )


# ── Paper Portfolio ───────────────────────────────────────────────────────────

@app.route("/positions", methods=["GET", "POST"])
def positions():
    from positions import (add_stock, add_option, add_spread,
                           remove_position_by_id, get_positions_web_data,
                           save_positions, load_positions)
    from alerts.positions_notifier import (notify_position_opened,
                                           notify_position_closed,
                                           notify_profit_target,
                                           notify_daily_pnl)

    error_msg   = None
    success_msg = None

    if request.method == "POST":
        action = request.form.get("action", "")

        # Save positions webhook if submitted
        pos_wh = request.form.get("positions_webhook", "").strip()
        if pos_wh:
            s = _load_settings()
            s["positions_webhook"] = pos_wh
            _save_settings(s)

        if action == "remove":
            idx = request.form.get("idx", "-1")
            try:
                idx_int = int(idx)
                # Grab position data before removing for the notification
                all_pos = load_positions()
                removed = all_pos[idx_int] if 0 <= idx_int < len(all_pos) else None
                remove_position_by_id(idx_int)
                success_msg = "Position removed."
                if removed:
                    try:
                        notify_position_closed(removed, pnl=0, pnl_pct=0,
                                               reason="Manually closed from portfolio")
                    except Exception:
                        pass
            except Exception as e:
                error_msg = str(e)

        elif action == "clear":
            save_positions([])
            success_msg = "All positions cleared."

        elif action == "daily_pnl":
            try:
                portfolio_data = get_positions_web_data()
                ok = notify_daily_pnl(portfolio_data)
                success_msg = "Daily P&L sent to Discord!" if ok else "Add a positions webhook URL above first."
            except Exception as e:
                error_msg = f"Error sending P&L: {e}"

        elif action == "add":
            pos_type  = request.form.get("pos_type", "spread")
            new_pos   = None
            try:
                if pos_type == "spread":
                    ticker       = request.form.get("ticker", "").strip().upper()
                    long_strike  = float(request.form.get("long_strike",  0))
                    short_strike = float(request.form.get("short_strike", 0))
                    opt_type     = request.form.get("opt_type", "call").strip().lower()
                    contracts    = int(request.form.get("contracts", 1))
                    debit_credit = request.form.get("debit_credit", "debit")
                    amount       = float(request.form.get("amount", 0))
                    net_cost     = amount if debit_credit == "debit" else -amount
                    expiry       = request.form.get("expiry", "").strip()
                    add_spread(ticker, long_strike, short_strike, opt_type,
                               contracts, net_cost, expiry)
                    new_pos = {"kind": "spread", "ticker": ticker,
                               "long_strike": long_strike, "short_strike": short_strike,
                               "option_type": opt_type, "contracts": contracts,
                               "net_cost": net_cost, "expiry": expiry}
                    success_msg = f"Added {ticker} {opt_type.upper()} ${long_strike:.0f}/{short_strike:.0f} spread."

                elif pos_type == "option":
                    ticker    = request.form.get("ticker", "").strip().upper()
                    strike    = float(request.form.get("strike", 0))
                    expiry    = request.form.get("expiry_single", "").strip()
                    opt_type  = request.form.get("opt_type_single", "call").strip().lower()
                    contracts = int(request.form.get("contracts_single", 1))
                    premium   = float(request.form.get("premium", 0))
                    add_option(ticker, strike, expiry, opt_type, contracts, premium)
                    new_pos = {"kind": "option", "ticker": ticker, "strike": strike,
                               "option_type": opt_type, "contracts": contracts,
                               "premium": premium, "expiry": expiry}
                    success_msg = f"Added {ticker} {opt_type.upper()} ${strike:.0f} option."

                elif pos_type == "stock":
                    ticker      = request.form.get("ticker", "").strip().upper()
                    entry_price = float(request.form.get("entry_price", 0))
                    shares      = float(request.form.get("shares", 0))
                    add_stock(ticker, entry_price, shares)
                    new_pos = {"kind": "stock", "ticker": ticker,
                               "entry_price": entry_price, "shares": shares}
                    success_msg = f"Added {ticker} stock position."

                # Fire Discord notification for new position
                if new_pos:
                    try:
                        notify_position_opened(new_pos)
                    except Exception:
                        pass

            except Exception as e:
                error_msg = f"Error adding position: {e}"

    # Always reload fresh data after any action
    try:
        portfolio = get_positions_web_data()
        # Check for 50% profit targets and fire alerts
        if portfolio:
            for p in portfolio.get("positions", []):
                if (not p.get("error") and p.get("pnl_pct", 0) >= 0.50
                        and p.get("status") != "CLOSE"):
                    try:
                        notify_profit_target(p, p.get("pnl", 0), p.get("pnl_pct", 0))
                    except Exception:
                        pass
    except Exception as e:
        portfolio = None
        error_msg = error_msg or f"Error loading portfolio: {e}"

    settings = _load_settings()
    return render_template("positions.html",
                           portfolio=portfolio,
                           success_msg=success_msg,
                           error_msg=error_msg,
                           positions_webhook=settings.get("positions_webhook", ""))


# ── Alpaca Paper Account ──────────────────────────────────────────────────────

@app.route("/alpaca")
def alpaca_status():
    """Show Alpaca paper account summary and positions."""
    from alpaca_connect import get_account_summary, get_positions
    summary   = get_account_summary()
    positions = get_positions() if not summary.get("error") else []
    return render_template("alpaca.html",
                           summary=summary,
                           positions=positions)


@app.route("/alpaca/order", methods=["POST"])
def alpaca_order():
    """Submit a paper market order via Alpaca."""
    symbol = request.form.get("symbol", "").strip().upper()
    qty    = request.form.get("qty",    "0").strip()
    side   = request.form.get("side",   "buy").strip()
    result = {}
    if symbol and qty:
        from alpaca_connect import submit_market_order
        try:
            result = submit_market_order(symbol, int(qty), side)
        except Exception as e:
            result = {"error": str(e)}
    return redirect(url_for("alpaca_status"))


# ── APScheduler — Auto Morning Briefing ──────────────────────────────────────

def _run_morning_briefing_job():
    """
    Scheduled job: scan watchlist + write Claude briefing + send to Discord.
    Runs at 9:00 AM ET Monday–Friday if auto_briefing is enabled in settings.
    """
    with app.app_context():
        try:
            s = _load_settings()
            if not s.get("auto_briefing"):
                return  # Auto-briefing is disabled
            webhook   = s.get("webhook_url", "").strip()
            watchlist = s.get("watchlist", DEFAULT_WATCHLIST).split()
            if not webhook or not webhook.startswith("https://discord.com/api/webhooks/"):
                return

            from alerts.scanner import scan_watchlist
            from conquest_brain  import morning_briefing
            from datetime import datetime, timezone
            import requests as _req

            scan_results = scan_watchlist(watchlist)

            # Enrich with live FRED macro context
            macro_notes = ""
            try:
                from macro.fred_data import fetch_fred_macro, fred_macro_context
                macro_notes = fred_macro_context(fetch_fred_macro())
            except Exception:
                pass

            briefing_text = morning_briefing(scan_results, macro_notes=macro_notes)
            embed = {
                "title":       "⚔️  Conquest Morning Briefing  —  Auto 9 AM ET",
                "description": briefing_text,
                "color":       0x7c6af7,
                "footer":      {"text": "Conquest Intelligence Desk  •  Automated"},
                "timestamp":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            resp = _req.post(webhook, json={"embeds": [embed]}, timeout=15)
            if resp.status_code == 204:
                print(f"[Scheduler] Morning briefing sent at {datetime.now()}")
            else:
                print(f"[Scheduler] Discord returned {resp.status_code}")
        except Exception as e:
            print(f"[Scheduler] Morning briefing job failed: {e}")


def _run_paper_generate_job():
    """APScheduler job: generate 10 paper trades at 9:35 AM ET weekdays."""
    with app.app_context():
        try:
            from paper_trader import generate_daily_trades
            new = generate_daily_trades(10)
            print(f"[Scheduler] Paper trade generation: {len(new)} new trades")
        except Exception as e:
            print(f"[Scheduler] Paper generate job failed: {e}")


def _run_paper_close_job():
    """APScheduler job: mark-to-market and close paper trades at 4:05 PM ET weekdays."""
    with app.app_context():
        try:
            from paper_trader import run_daily_close
            result = run_daily_close()
            print(f"[Scheduler] Paper close job: {result}")
        except Exception as e:
            print(f"[Scheduler] Paper close job failed: {e}")


def _start_scheduler():
    """Start APScheduler background scheduler for automated jobs."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron         import CronTrigger

        scheduler = BackgroundScheduler()

        # Morning briefing — 9:00 AM ET
        scheduler.add_job(
            _run_morning_briefing_job,
            CronTrigger(day_of_week="mon-fri", hour=9, minute=0,
                        timezone="America/New_York"),
            id="morning_briefing",
            replace_existing=True,
            misfire_grace_time=300,
        )
        # Paper trade generation — 9:35 AM ET (after open stabilises)
        scheduler.add_job(
            _run_paper_generate_job,
            CronTrigger(day_of_week="mon-fri", hour=9, minute=35,
                        timezone="America/New_York"),
            id="paper_generate",
            replace_existing=True,
            misfire_grace_time=300,
        )
        # Paper trade close — 4:05 PM ET (5 min after close)
        scheduler.add_job(
            _run_paper_close_job,
            CronTrigger(day_of_week="mon-fri", hour=16, minute=5,
                        timezone="America/New_York"),
            id="paper_close",
            replace_existing=True,
            misfire_grace_time=300,
        )

        scheduler.start()
        print("✓ Scheduler running — briefing 9:00 AM · paper trades 9:35 AM / 4:05 PM ET (Mon–Fri)")
        return scheduler
    except Exception as e:
        print(f"  Scheduler not started: {e}")
        return None


# ── Paper Trading Stats ───────────────────────────────────────────────────────

@app.route("/paper-stats")
def paper_stats():
    from paper_trader import get_paper_stats
    stats = get_paper_stats()
    return render_template("paper_stats.html", stats=stats,
                           flash_ok=None, flash_err=None)


@app.route("/paper-stats/generate", methods=["POST"])
def paper_stats_generate():
    flash_ok = flash_err = None
    try:
        from paper_trader import generate_daily_trades
        new = generate_daily_trades(10)
        if new:
            flash_ok = f"Generated {len(new)} paper trades for today."
        else:
            flash_ok = "Today's trades already exist (or no data available). Nothing added."
    except Exception as e:
        flash_err = f"Generation failed: {e}"
    from paper_trader import get_paper_stats
    stats = get_paper_stats()
    return render_template("paper_stats.html", stats=stats,
                           flash_ok=flash_ok, flash_err=flash_err)


@app.route("/paper-stats/close", methods=["POST"])
def paper_stats_close():
    flash_ok = flash_err = None
    try:
        from paper_trader import run_daily_close
        result  = run_daily_close()
        flash_ok = (f"Mark-to-market complete — "
                    f"{result['closed']} trades closed, "
                    f"{result['still_open']} still open.")
    except Exception as e:
        flash_err = f"Close run failed: {e}"
    from paper_trader import get_paper_stats
    stats = get_paper_stats()
    return render_template("paper_stats.html", stats=stats,
                           flash_ok=flash_ok, flash_err=flash_err)


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser, threading

    # Cloud deployments (Railway/Render) set the PORT environment variable
    port       = int(os.environ.get("PORT", 5000))
    is_cloud   = os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER")
    debug_mode = not is_cloud   # debug off in production
    use_reload = not is_cloud   # reloader off in production

    # Start the scheduler only in the actual Flask worker process (not the reloader wrapper)
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or is_cloud:
        _start_scheduler()

    if not is_cloud:
        def open_browser():
            webbrowser.open(f"http://localhost:{port}")
        threading.Timer(1.2, open_browser).start()

    print(f"Starting Conquest Trading at http://localhost:{port}")
    if is_cloud:
        print("Running in cloud mode — debug off, browser auto-open disabled.")
    print("Press Ctrl+C to stop.")
    app.run(debug=debug_mode, use_reloader=use_reload, host="0.0.0.0", port=port)
