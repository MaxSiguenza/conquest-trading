import sys
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import numpy as np

from backtest.engine import BacktestEngine
from config import Config, DataConfig
from data.fetcher import fetch_ohlcv, fetch_vix, get_earnings_dates
from forecast.monte_carlo import run_monte_carlo, forecast_summary
from models.black_scholes import black_scholes_price, option_greeks
from models.kelly_criterion import kelly_criterion
from signals.generator import generate_signals


def run(cfg: Config = None) -> None:
    if cfg is None:
        if len(sys.argv) > 1:
            ticker = sys.argv[1].upper()
        else:
            ticker = input("Enter ticker symbol (e.g. AAPL, TSLA, NVDA): ").strip().upper()
        cfg = Config(data=DataConfig(ticker=ticker))

    # ------------------------------------------------------------------ #
    #  1. Data                                                             #
    # ------------------------------------------------------------------ #
    print(f"\nFetching {cfg.data.ticker} ({cfg.data.start_date} → {cfg.data.end_date})...")
    df = fetch_ohlcv(cfg.data.ticker, cfg.data.start_date, cfg.data.end_date)
    print(f"  {len(df)} trading days loaded.")

    print("Fetching VIX (market fear index)...")
    vix = fetch_vix(cfg.data.start_date, cfg.data.end_date)
    print(f"  VIX loaded. Current level: {vix.iloc[-1]:.1f}" if not vix.empty else "  VIX unavailable.")

    print(f"Fetching earnings dates for {cfg.data.ticker}...")
    earnings_dates = get_earnings_dates(cfg.data.ticker)
    print(f"  {len(earnings_dates)} earnings events found.")

    # ------------------------------------------------------------------ #
    #  2. Signals                                                          #
    # ------------------------------------------------------------------ #
    print("\nGenerating signals...")
    df = generate_signals(df, cfg.indicators, vix=vix, earnings_dates=earnings_dates)

    long_days   = (df["Position"] == 1).sum()
    vix_blocked = (df.get("VIX", pd.Series(dtype=float)) > cfg.indicators.vix_threshold).sum()
    warmup_days = df["SMA_Long"].isna().sum()

    print(f"  Warmup period (SMA {cfg.indicators.sma_long}): {warmup_days} days excluded")
    print(f"  VIX filter blocked entries on: {vix_blocked} days (VIX > {cfg.indicators.vix_threshold})")
    print(f"  Earnings blackout windows: {len(earnings_dates)} events × {cfg.indicators.earnings_buffer_days} days")
    print(f"  In-market: {long_days} of {len(df)} days ({long_days/len(df):.1%})")

    # ------------------------------------------------------------------ #
    #  3. Position sizing via Kelly                                        #
    # ------------------------------------------------------------------ #
    kelly_pct = kelly_criterion(win_prob=0.55, win_loss_ratio=1.5, fraction=cfg.risk.kelly_fraction)
    print(f"\nKelly position size: {kelly_pct:.2%} of portfolio per trade")

    # ------------------------------------------------------------------ #
    #  4. Options analysis                                                 #
    # ------------------------------------------------------------------ #
    S     = float(df["Close"].iloc[-1])
    K     = S * (1 + cfg.options.otm_pct)
    r     = cfg.options.risk_free_rate
    sigma = cfg.options.implied_vol

    print(f"\nOptions analysis — 5%-OTM call on {cfg.data.ticker} at ${S:.2f}:")
    print(f"  {'DTE':<6} {'Price':>8} {'Delta':>8} {'Theta/day':>10} {'Vega/1%':>10}  Note")
    print(f"  {'-'*60}")
    for dte, note in [(30, "too short"), (60, "borderline"), (90, "recommended"), (120, "conservative")]:
        T = dte / 365
        p = black_scholes_price(S, K, T, r, sigma, "call")
        g = option_greeks(S, K, T, r, sigma)
        print(f"  {dte:<6} ${p:>7.2f} {g['delta']:>8.4f} {g['theta']:>10.4f} {g['vega']:>10.4f}  ← {note}")

    # ------------------------------------------------------------------ #
    #  5. Monte Carlo forecast                                             #
    # ------------------------------------------------------------------ #
    print(f"\nRunning Monte Carlo forecast (1,000 simulations, {cfg.options.dte} days)...")
    mc   = run_monte_carlo(df["Close"], days=cfg.options.dte, simulations=1000)
    fsum = forecast_summary(mc, days_out=cfg.options.dte)

    print(f"  Current price   : ${fsum['current']:.2f}")
    print(f"  Median ({cfg.options.dte}d)    : ${fsum['median']:.2f}  ({(fsum['median']/fsum['current']-1)*100:+.1f}%)")
    print(f"  Bull case (90%) : ${fsum['bull']:.2f}  ({(fsum['bull']/fsum['current']-1)*100:+.1f}%)")
    print(f"  Bear case (10%) : ${fsum['bear']:.2f}  ({(fsum['bear']/fsum['current']-1)*100:+.1f}%)")
    print(f"  Prob above today: {fsum['prob_up']:.1%}")
    print(f"  Implied vol     : {fsum['implied_vol']:.1%} annualized")

    # ------------------------------------------------------------------ #
    #  6. Backtest                                                         #
    # ------------------------------------------------------------------ #
    print("\nRunning backtest...")
    engine = BacktestEngine(cfg.backtest, cfg.risk)
    result = engine.run(df.copy(), kelly_pct)

    metrics = engine.performance_metrics(result["Equity"])
    print("\n" + "=" * 42)
    print("  PERFORMANCE SUMMARY")
    print("=" * 42)
    for k, v in metrics.items():
        print(f"  {k:<22} {v}")
    print("=" * 42)

    # ------------------------------------------------------------------ #
    #  7. Charts — history + forecast on same price panel                  #
    # ------------------------------------------------------------------ #
    fig = plt.figure(figsize=(16, 22))
    fig.suptitle(
        f"{cfg.data.ticker} — Position Trading Strategy + {cfg.options.dte}-Day Monte Carlo Forecast",
        fontsize=13, fontweight="bold", y=0.98,
    )

    # Use gridspec so the forecast panel is taller
    gs = fig.add_gridspec(6, 1, hspace=0.35)
    ax_price    = fig.add_subplot(gs[0:2])  # tall — price + forecast
    ax_vix      = fig.add_subplot(gs[2], sharex=ax_price)
    ax_rsi      = fig.add_subplot(gs[3], sharex=ax_price)
    ax_macd     = fig.add_subplot(gs[4], sharex=ax_price)
    ax_equity   = fig.add_subplot(gs[5], sharex=ax_price)

    # ── Panel 0: Price + MAs + trades + forecast cone ──────────────────
    # Shade background green when all 3 timeframes agree bullish
    if "MTF_Score" in result.columns:
        all_aligned = result["MTF_Score"] >= 3
        ax_price.fill_between(result.index, result["Close"].min() * 0.8,
                              result["Close"].max() * 1.2,
                              where=all_aligned, color="green", alpha=0.06,
                              label="All 3 TF aligned")

    ax_price.plot(result["Close"],     label="Price",                    alpha=0.8, linewidth=1,   color="black")
    ax_price.plot(result["SMA_Short"], label=f"SMA {cfg.indicators.sma_short}", alpha=0.9, linewidth=1.3, color="steelblue")
    ax_price.plot(result["SMA_Long"],  label=f"SMA {cfg.indicators.sma_long}",  alpha=0.9, linewidth=1.6, color="darkorange")

    buys  = [t for t in engine.trades if t.action == "BUY"]
    sells = [t for t in engine.trades if t.action == "SELL"]
    if buys:
        ax_price.scatter([t.date for t in buys],  [t.price for t in buys],
                         marker="^", color="green", zorder=5, label="Buy",  s=80)
    if sells:
        ax_price.scatter([t.date for t in sells], [t.price for t in sells],
                         marker="v", color="red",   zorder=5, label="Sell", s=80)

    # Forecast cone — shaded percentile bands
    fd = mc["future_dates"]
    ax_price.fill_between(fd, mc["p10"], mc["p90"], alpha=0.10, color="royalblue", label="10–90% range")
    ax_price.fill_between(fd, mc["p25"], mc["p75"], alpha=0.20, color="royalblue", label="25–75% range")
    ax_price.plot(fd, mc["p50"], linestyle="--", linewidth=1.4, color="royalblue", label="Median forecast", alpha=0.9)

    # Strike price line for the recommended option
    ax_price.axhline(K, linestyle=":", linewidth=1, color="purple", alpha=0.7,
                     label=f"Option strike (${K:.0f})")

    # Vertical line separating history from forecast
    ax_price.axvline(df.index[-1], color="gray", linewidth=1, linestyle="--", alpha=0.6)
    ax_price.text(fd[0], ax_price.get_ylim()[0] if ax_price.get_ylim()[0] != 0 else S * 0.7,
                  " Forecast →", fontsize=8, color="gray", va="bottom")

    # Stats annotation box
    stats_text = (
        f"{cfg.options.dte}-day forecast\n"
        f"─────────────────\n"
        f"Current:  ${fsum['current']:.2f}\n"
        f"Median:   ${fsum['median']:.2f}  ({(fsum['median']/fsum['current']-1)*100:+.1f}%)\n"
        f"Bull 90%: ${fsum['bull']:.2f}  ({(fsum['bull']/fsum['current']-1)*100:+.1f}%)\n"
        f"Bear 10%: ${fsum['bear']:.2f}  ({(fsum['bear']/fsum['current']-1)*100:+.1f}%)\n"
        f"Prob up:  {fsum['prob_up']:.1%}\n"
        f"Ann. vol: {fsum['implied_vol']:.1%}"
    )
    ax_price.text(
        0.01, 0.97, stats_text,
        transform=ax_price.transAxes,
        fontsize=8, verticalalignment="top",
        bbox=dict(boxstyle="round,pad=0.4", facecolor="white", alpha=0.85, edgecolor="royalblue"),
        fontfamily="monospace",
    )

    ax_price.set_ylabel("Price ($)")
    ax_price.legend(fontsize=7, loc="upper left", bbox_to_anchor=(0.0, 0.72))
    ax_price.grid(alpha=0.3)

    # ── MTF score annotation ───────────────────────────────────────────
    if "MTF_Score" in result.columns:
        last = result.iloc[-1]
        mtf  = int(last.get("MTF_Score", 0))
        d    = "BULL" if last.get("Regime", 0)   == 1 else "BEAR"
        w    = "BULL" if last.get("W_Regime", 0) == 1 else "BEAR"
        m    = "BULL" if last.get("M_Regime", 0) == 1 else "BEAR"
        mtf_text = (
            f"Multi-Timeframe: {mtf}/3\n"
            f"Monthly: {m}  Weekly: {w}  Daily: {d}"
        )
        color = "green" if mtf == 3 else "orange" if mtf == 2 else "red"
        ax_price.text(
            0.99, 0.97, mtf_text,
            transform=ax_price.transAxes, fontsize=8,
            verticalalignment="top", horizontalalignment="right",
            bbox=dict(boxstyle="round,pad=0.4", facecolor=color, alpha=0.25, edgecolor=color),
            fontfamily="monospace",
        )

    # ── Panel 1: VIX ───────────────────────────────────────────────────
    if "VIX" in result.columns:
        ax_vix.plot(result["VIX"], label="VIX", color="darkorange", linewidth=1)
        ax_vix.axhline(cfg.indicators.vix_threshold, color="red", linestyle="--",
                       alpha=0.7, linewidth=0.9, label=f"Panic threshold ({cfg.indicators.vix_threshold})")
        ax_vix.fill_between(result.index, cfg.indicators.vix_threshold, result["VIX"],
                            where=result["VIX"] > cfg.indicators.vix_threshold,
                            color="red", alpha=0.15)
        ax_vix.set_ylabel("VIX")
        ax_vix.legend(fontsize=7)
        ax_vix.grid(alpha=0.3)

    # ── Panel 2: RSI ───────────────────────────────────────────────────
    ax_rsi.plot(result["RSI"], label=f"RSI ({cfg.indicators.rsi_period})", color="purple", linewidth=1)
    ax_rsi.axhline(cfg.indicators.rsi_overbought, color="red",   linestyle="--", alpha=0.6, linewidth=0.8)
    ax_rsi.axhline(cfg.indicators.rsi_oversold,   color="green", linestyle="--", alpha=0.6, linewidth=0.8)
    ax_rsi.fill_between(result.index, cfg.indicators.rsi_overbought, result["RSI"],
                        where=result["RSI"] > cfg.indicators.rsi_overbought, color="red",   alpha=0.1)
    ax_rsi.fill_between(result.index, cfg.indicators.rsi_oversold,   result["RSI"],
                        where=result["RSI"] < cfg.indicators.rsi_oversold,  color="green", alpha=0.1)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI")
    ax_rsi.legend(fontsize=7)
    ax_rsi.grid(alpha=0.3)

    # ── Panel 3: MACD ──────────────────────────────────────────────────
    ax_macd.plot(result["MACD_Line"],   label="MACD",   linewidth=1)
    ax_macd.plot(result["MACD_Signal"], label="Signal", linewidth=1)
    colors = ["green" if v >= 0 else "red" for v in result["MACD_Hist"]]
    ax_macd.bar(result.index, result["MACD_Hist"], label="Histogram", color=colors, alpha=0.5, width=1)
    ax_macd.axhline(0, color="black", linewidth=0.5)
    ax_macd.set_ylabel("MACD")
    ax_macd.legend(fontsize=7)
    ax_macd.grid(alpha=0.3)

    # ── Panel 4: Equity curve ──────────────────────────────────────────
    ax_equity.plot(result["Equity"], label="Strategy", color="navy", linewidth=1.2)
    ax_equity.axhline(cfg.backtest.initial_capital, color="gray", linestyle="--",
                      alpha=0.6, linewidth=0.8, label="Starting Capital ($100k)")
    ax_equity.fill_between(result.index, cfg.backtest.initial_capital, result["Equity"],
                           where=result["Equity"] >= cfg.backtest.initial_capital,
                           color="green", alpha=0.1)
    ax_equity.fill_between(result.index, cfg.backtest.initial_capital, result["Equity"],
                           where=result["Equity"] < cfg.backtest.initial_capital,
                           color="red", alpha=0.1)
    ax_equity.set_ylabel("Portfolio ($)")
    ax_equity.legend(fontsize=7)
    ax_equity.grid(alpha=0.3)

    plt.savefig(f"{cfg.data.ticker}_analysis.png", dpi=150, bbox_inches="tight")
    print(f"\nChart saved as {cfg.data.ticker}_analysis.png")
    plt.show()


if __name__ == "__main__":
    run()
