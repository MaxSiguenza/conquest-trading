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

# ── One-time migration: seed PostgreSQL from local JSON files on first deploy ─
try:
    from db import migrate_files_to_db, db_available
    if db_available():
        migrate_files_to_db()
except Exception as _db_err:
    print(f"[DB] Startup migration skipped: {_db_err}")
DEFAULT_WATCHLIST = "AAPL NVDA MSFT GOOGL AMZN META TSLA JPM XOM WMT COP SPY QQQ"


def _load_settings() -> dict:
    from db import kv_get
    data = kv_get("settings")
    if not isinstance(data, dict):
        return {"webhook_url": "", "watchlist": DEFAULT_WATCHLIST, "auto_briefing": True}
    return data


def _save_settings(data: dict):
    from db import kv_set
    kv_set("settings", data)


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

        if action == "save":
            # Settings already saved above — just redirect back with confirmation
            from flask import redirect, url_for
            return redirect("/alerts?saved=1")

        elif action == "test":
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

    saved = request.args.get("saved") == "1"

    return render_template(
        "alerts.html",
        settings=settings,
        scan_results=scan_results,
        send_status=send_status,
        action=action,
        auto_briefing=settings.get("auto_briefing", False),
        saved=saved,
    )


# ── Paper Portfolio ───────────────────────────────────────────────────────────

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


def _run_brief_generation_job():
    """APScheduler job: collect all data and pre-generate the morning brief at 8:45 AM ET."""
    with app.app_context():
        try:
            from morning_brief import generate_brief
            import pytz
            s         = _load_settings()
            watchlist = s.get("watchlist", DEFAULT_WATCHLIST).split()
            brief     = generate_brief(watchlist=watchlist, force=True)
            print(f"[Scheduler] Morning Intelligence Brief generated at "
                  f"{brief.get('generated_at', '')[:16]}")
        except Exception as e:
            print(f"[Scheduler] Brief generation job failed: {e}")


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

        # Morning Intelligence Brief — 8:45 AM ET (ready before Discord posts at 9 AM)
        scheduler.add_job(
            _run_brief_generation_job,
            CronTrigger(day_of_week="mon-fri", hour=8, minute=45,
                        timezone="America/New_York"),
            id="morning_brief",
            replace_existing=True,
            misfire_grace_time=300,
        )
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
        print("✓ Scheduler running — brief 8:45 AM · briefing 9:00 AM · "
              "paper trades 9:35 AM / 4:05 PM ET (Mon–Fri)")
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


# ── Watchlist ─────────────────────────────────────────────────────────────────

@app.route("/watchlist")
def watchlist_page():
    from watchlist_engine import load_watchlist
    entries = load_watchlist()
    return render_template("watchlist.html", entries=entries)


@app.route("/watchlist/add", methods=["POST"])
def watchlist_add():
    ticker = request.form.get("ticker", "").strip().upper()
    if not ticker:
        return redirect(url_for("watchlist_page"))
    try:
        from watchlist_engine import analyze_and_add
        analyze_and_add(ticker)
    except Exception as e:
        pass  # entry will be missing — page will still load fine
    return redirect(url_for("watchlist_page"))


@app.route("/watchlist/remove", methods=["POST"])
def watchlist_remove():
    ticker = request.form.get("ticker", "").strip().upper()
    if ticker:
        from watchlist_engine import remove_entry
        remove_entry(ticker)
    return redirect(url_for("watchlist_page"))


# ── Morning Intelligence Brief ────────────────────────────────────────────────

@app.route("/brief")
def brief_page():
    from morning_brief import generate_brief
    from datetime import datetime
    import pytz

    s         = _load_settings()
    watchlist = s.get("watchlist", DEFAULT_WATCHLIST).split()
    brief     = generate_brief(watchlist=watchlist)

    # Format generated_at → human-readable ET time
    gen_at = brief.get("generated_at", "")
    if gen_at:
        try:
            dt = datetime.fromisoformat(gen_at)
            et = dt.astimezone(pytz.timezone("America/New_York"))
            brief["generated_at_display"] = et.strftime("%I:%M %p ET").lstrip("0")
        except Exception:
            brief["generated_at_display"] = gen_at[:16]
    else:
        brief["generated_at_display"] = ""

    # Format market_date → "May 21, 2026"
    md = brief.get("market_date", "")
    try:
        brief["market_date_display"] = datetime.strptime(md, "%Y-%m-%d").strftime("%B %d, %Y")
    except Exception:
        brief["market_date_display"] = md

    return render_template("brief.html", brief=brief)


@app.route("/brief/generate", methods=["POST"])
def brief_generate():
    from morning_brief import generate_brief
    s         = _load_settings()
    watchlist = s.get("watchlist", DEFAULT_WATCHLIST).split()
    generate_brief(watchlist=watchlist, force=True)
    return redirect(url_for("brief_page"))


# ── Backtest ──────────────────────────────────────────────────────────────────

@app.route("/backtest", methods=["GET", "POST"])
def backtest_page():
    """
    Run the Conquest backtesting engine.
    GET  → show the form
    POST → fire backtest in a background thread, redirect back with status
    """
    from flask import flash, get_flashed_messages
    status = None
    error  = None

    if request.method == "POST":
        period  = request.form.get("period", "2y").strip() or "2y"
        raw     = request.form.get("tickers", "").strip().upper()
        tickers = raw.split() if raw else None

        def _run_bg():
            try:
                import sys
                sys.path.insert(0, APP_DIR)
                from backtest import run_backtest
                run_backtest(
                    tickers=tickers,
                    period=period,
                    workers=10,
                    discord=True,
                )
            except Exception as bg_e:
                print(f"[Backtest] Background run failed: {bg_e}")

        import threading
        threading.Thread(target=_run_bg, daemon=True).start()

        universe = f"{len(tickers)} tickers" if tickers else "full 129-ticker universe"
        status = (
            f"Backtest started on {universe} ({period} lookback). "
            f"Results will be posted to #agent-brain on Discord in a few minutes."
        )

    return render_template("backtest.html", status=status, error=error)


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
