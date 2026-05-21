# -*- coding: utf-8 -*-
"""
Chart Generator
================
Generates an interactive Plotly chart and returns it as an HTML fragment
so it can be embedded directly in the web app.
"""
import sys
sys.path.insert(0, ".")

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from config import Config, DataConfig
from data.fetcher import fetch_ohlcv, fetch_vix, get_earnings_dates
from signals.generator import generate_signals
from indicators.volatility import calculate_hv_rank, calculate_adx
from forecast.monte_carlo import run_monte_carlo, forecast_summary
from backtest.engine import BacktestEngine
from models.kelly_criterion import kelly_criterion

# Theme colors matching the web app
BG_DARK   = "#0f1117"
BG_CARD   = "#1a1d27"
BG_BORDER = "#2d3148"
FG_TEXT   = "#c9d6e8"
FG_DIM    = "#475569"
PURPLE    = "#7c6af7"
GREEN     = "#4ade80"
RED       = "#f87171"
ORANGE    = "#fb923c"
BLUE      = "#60a5fa"


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def generate_chart(ticker: str) -> tuple[str, dict]:
    """
    Generate an interactive Plotly chart for a ticker.
    Returns (plotly_html_fragment, stats_dict).
    """
    cfg = Config(data=DataConfig(ticker=ticker))

    df             = fetch_ohlcv(ticker, cfg.data.start_date, cfg.data.end_date)
    vix            = fetch_vix(cfg.data.start_date, cfg.data.end_date)
    earnings_dates = get_earnings_dates(ticker)
    df             = generate_signals(df, cfg.indicators, vix=vix, earnings_dates=earnings_dates)

    # Key stats
    current     = float(df["Close"].iloc[-1])
    hvr         = calculate_hv_rank(df)
    adx_df      = calculate_adx(df)
    adx_val     = float(adx_df["ADX"].iloc[-1]) if not adx_df.empty else 0
    last        = df.iloc[-1]
    mtf_score   = int(last.get("MTF_Score", 0))
    daily_reg   = "BULL" if last.get("Regime", 0)   == 1 else "BEAR"
    weekly_reg  = "BULL" if last.get("W_Regime", 0) == 1 else "BEAR"
    monthly_reg = "BULL" if last.get("M_Regime", 0) == 1 else "BEAR"
    entry_sig   = bool(last.get("Entry_Signal", 0))
    rsi_val     = float(last.get("RSI", 50))

    # Monte Carlo
    mc   = run_monte_carlo(df["Close"], days=cfg.options.dte, simulations=1000)
    fsum = forecast_summary(mc, days_out=cfg.options.dte)

    # Backtest
    kelly_pct = kelly_criterion(win_prob=0.55, win_loss_ratio=1.5, fraction=cfg.risk.kelly_fraction)
    engine    = BacktestEngine(cfg.backtest, cfg.risk)
    result    = engine.run(df.copy(), kelly_pct)
    metrics   = engine.performance_metrics(result["Equity"])

    # ── Build Plotly chart ────────────────────────────────────────────────────
    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=[0.46, 0.17, 0.17, 0.20],
        subplot_titles=("Price & Indicators", "RSI", "MACD", "Equity Curve"),
    )

    axis_style = dict(
        gridcolor=BG_BORDER,
        zerolinecolor=BG_BORDER,
        showspikes=True,
        spikecolor=FG_DIM,
        spikethickness=1,
        tickfont=dict(size=10, color=FG_DIM),
    )

    # ── Price panel ──────────────────────────────────────────────────────────

    # MTF bull highlight zone
    if "MTF_Score" in result.columns:
        bull_mask = result["MTF_Score"] >= 3
        # Shade each bull run as a filled area near zero opacity
        in_bull = False
        start_x = None
        for i, (idx, row) in enumerate(result.iterrows()):
            if bull_mask.iloc[i] and not in_bull:
                in_bull = True
                start_x = idx
            elif not bull_mask.iloc[i] and in_bull:
                in_bull = False
                fig.add_vrect(x0=start_x, x1=idx,
                              fillcolor=_rgba(GREEN, 0.04),
                              layer="below", line_width=0, row=1, col=1)
        if in_bull and start_x is not None:
            fig.add_vrect(x0=start_x, x1=result.index[-1],
                          fillcolor=_rgba(GREEN, 0.04),
                          layer="below", line_width=0, row=1, col=1)

    # Bollinger Bands — draw first so price line renders on top
    if "BB_Upper" in result.columns:
        fig.add_trace(go.Scatter(
            x=result.index, y=result["BB_Upper"],
            name="BB Upper", line=dict(color=PURPLE, width=0.8, dash="dash"),
            opacity=0.45, showlegend=True,
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=result.index, y=result["BB_Lower"],
            name="BB Lower", line=dict(color=PURPLE, width=0.8, dash="dash"),
            opacity=0.45, fill="tonexty",
            fillcolor=_rgba(PURPLE, 0.05), showlegend=False,
        ), row=1, col=1)

    # EMAs
    fig.add_trace(go.Scatter(
        x=result.index, y=result["SMA_Short"],
        name=f"EMA {cfg.indicators.sma_short}",
        line=dict(color=BLUE, width=1.3), opacity=0.9,
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=result.index, y=result["SMA_Long"],
        name=f"EMA {cfg.indicators.sma_long}",
        line=dict(color=ORANGE, width=1.5), opacity=0.9,
    ), row=1, col=1)

    # Price line
    fig.add_trace(go.Scatter(
        x=result.index, y=result["Close"],
        name="Price", line=dict(color=FG_TEXT, width=1.5),
        hovertemplate="<b>%{x|%b %d %Y}</b><br>Price: $%{y:.2f}<extra></extra>",
    ), row=1, col=1)

    # Monte Carlo cone
    fd    = list(mc["future_dates"])
    p10   = list(mc["p10"])
    p25   = list(mc["p25"])
    p75   = list(mc["p75"])
    p90   = list(mc["p90"])
    p50   = list(mc["p50"])

    fig.add_trace(go.Scatter(
        x=fd + fd[::-1], y=p90 + p10[::-1],
        fill="toself", fillcolor=_rgba(BLUE, 0.07),
        line=dict(color="rgba(0,0,0,0)"),
        name="MC 10-90%", showlegend=True,
        hoverinfo="skip",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=fd + fd[::-1], y=p75 + p25[::-1],
        fill="toself", fillcolor=_rgba(BLUE, 0.14),
        line=dict(color="rgba(0,0,0,0)"),
        name="MC 25-75%", showlegend=True,
        hoverinfo="skip",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=fd, y=p50,
        name=f"Median ${fsum['median']:.0f}",
        line=dict(color=BLUE, width=1.5, dash="dash"),
        hovertemplate="Forecast: $%{y:.2f}<extra></extra>",
    ), row=1, col=1)

    # Current date divider
    fig.add_vline(x=df.index[-1], line=dict(color=FG_DIM, width=1, dash="dot"),
                  row=1, col=1)

    # MACD bullish crossover markers (add / re-entry signals)
    cross_up   = result[result["MACD_Cross_Up"]   == 1] if "MACD_Cross_Up"   in result.columns else pd.DataFrame()
    cross_down = result[result["MACD_Cross_Down"] == 1] if "MACD_Cross_Down" in result.columns else pd.DataFrame()
    if not cross_up.empty:
        fig.add_trace(go.Scatter(
            x=cross_up.index, y=cross_up["Close"],
            mode="markers", name="MACD Cross ↑",
            marker=dict(symbol="diamond", color=ORANGE, size=9, line=dict(width=1, color="#0f1117")),
            hovertemplate="MACD Cross: $%{y:.2f}<extra></extra>",
        ), row=1, col=1)
    if not cross_down.empty:
        fig.add_trace(go.Scatter(
            x=cross_down.index, y=cross_down["Close"],
            mode="markers", name="MACD Cross ↓",
            marker=dict(symbol="diamond", color="#94a3b8", size=7, line=dict(width=1, color="#0f1117")),
            hovertemplate="MACD Bear Cross: $%{y:.2f}<extra></extra>",
        ), row=1, col=1)

    # Backtest entry / exit trades
    buys  = [t for t in engine.trades if t.action == "BUY"]
    sells = [t for t in engine.trades if t.action == "SELL"]
    if buys:
        fig.add_trace(go.Scatter(
            x=[t.date for t in buys], y=[t.price for t in buys],
            mode="markers", name="Entry",
            marker=dict(symbol="triangle-up", color=GREEN, size=13, line=dict(width=1.5, color="#0f1117")),
            hovertemplate="Entry: $%{y:.2f}<extra></extra>",
        ), row=1, col=1)
    if sells:
        fig.add_trace(go.Scatter(
            x=[t.date for t in sells], y=[t.price for t in sells],
            mode="markers", name="Exit",
            marker=dict(symbol="triangle-down", color=RED, size=13, line=dict(width=1.5, color="#0f1117")),
            hovertemplate="Exit: $%{y:.2f}<extra></extra>",
        ), row=1, col=1)

    # MTF label annotation
    mtf_color = GREEN if mtf_score == 3 else ORANGE if mtf_score == 2 else RED
    fig.add_annotation(
        text=f"MTF {mtf_score}/3 | M:{monthly_reg} W:{weekly_reg} D:{daily_reg}",
        xref="paper", yref="paper", x=0.99, y=0.99,
        xanchor="right", yanchor="top",
        font=dict(size=11, color=mtf_color, family="monospace"),
        bgcolor=BG_DARK, bordercolor=mtf_color, borderwidth=1,
        showarrow=False, row=1, col=1,
    )

    # ── RSI panel ────────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=result.index, y=result["RSI"],
        name="RSI", line=dict(color=PURPLE, width=1.2),
        hovertemplate="RSI: %{y:.1f}<extra></extra>",
    ), row=2, col=1)
    fig.add_hrect(y0=cfg.indicators.rsi_overbought, y1=100,
                  fillcolor=_rgba(RED, 0.06), line_width=0, row=2, col=1)
    fig.add_hrect(y0=0, y1=cfg.indicators.rsi_oversold,
                  fillcolor=_rgba(GREEN, 0.06), line_width=0, row=2, col=1)
    for lvl, color in [(cfg.indicators.rsi_overbought, RED),
                       (cfg.indicators.rsi_oversold, GREEN),
                       (50, FG_DIM)]:
        fig.add_hline(y=lvl, line=dict(color=color, width=0.7, dash="dash"),
                      row=2, col=1)

    # ── MACD panel ───────────────────────────────────────────────────────────
    hist_colors = [GREEN if v >= 0 else RED for v in result["MACD_Hist"]]
    fig.add_trace(go.Bar(
        x=result.index, y=result["MACD_Hist"],
        name="MACD Hist", marker_color=hist_colors, opacity=0.55,
        hovertemplate="Hist: %{y:.4f}<extra></extra>",
    ), row=3, col=1)
    fig.add_trace(go.Scatter(
        x=result.index, y=result["MACD_Line"],
        name="MACD", line=dict(color=BLUE, width=1.2),
        hovertemplate="MACD: %{y:.4f}<extra></extra>",
    ), row=3, col=1)
    fig.add_trace(go.Scatter(
        x=result.index, y=result["MACD_Signal"],
        name="Signal", line=dict(color=ORANGE, width=1.2),
        hovertemplate="Signal: %{y:.4f}<extra></extra>",
    ), row=3, col=1)
    fig.add_hline(y=0, line=dict(color=FG_DIM, width=0.5), row=3, col=1)

    # ── Equity curve ─────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=result.index, y=result["Equity"],
        name="Strategy", line=dict(color=PURPLE, width=1.5),
        fill="tozeroy", fillcolor=_rgba(PURPLE, 0.07),
        hovertemplate="Equity: $%{y:,.0f}<extra></extra>",
    ), row=4, col=1)
    fig.add_hline(
        y=cfg.backtest.initial_capital,
        line=dict(color=FG_DIM, width=0.8, dash="dash"),
        annotation_text=f"Start ${cfg.backtest.initial_capital:,.0f}",
        annotation_font=dict(size=9, color=FG_DIM),
        row=4, col=1,
    )

    # ── Layout ───────────────────────────────────────────────────────────────
    # Default view: last 2 years + forecast window (zoom out to see full history)
    view_start = df.index[-1] - pd.Timedelta(days=730)
    view_end   = pd.Timestamp(mc["future_dates"][-1]) + pd.Timedelta(days=10)

    fig.update_layout(
        paper_bgcolor=BG_DARK,
        plot_bgcolor=BG_CARD,
        font=dict(color=FG_TEXT, family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif", size=11),
        title=dict(
            text=f"<b>{ticker}</b>  —  ${current:.2f}  |  MTF {mtf_score}/3  |"
                 f"  HVR {hvr['hv_rank']:.0f}/100  |  ADX {adx_val:.0f}",
            font=dict(size=14, color=FG_TEXT),
            x=0.02,
        ),
        height=940,
        legend=dict(
            bgcolor=BG_DARK, bordercolor=BG_BORDER, borderwidth=1,
            font=dict(size=10), orientation="h",
            yanchor="bottom", y=1.01, xanchor="left", x=0,
        ),
        margin=dict(l=60, r=20, t=80, b=40),
        hovermode="x unified",
        hoverlabel=dict(bgcolor=BG_DARK, bordercolor=BG_BORDER, font=dict(size=11)),
        dragmode="zoom",
    )

    # Apply y-axis styles to all subplots
    for i in range(1, 5):
        fig.update_yaxes(axis_style, row=i, col=1)

    fig.update_yaxes(title_text="Price ($)", title_font=dict(size=10, color=FG_DIM), autorange=True, fixedrange=False, row=1, col=1)
    fig.update_yaxes(title_text="RSI",       title_font=dict(size=10, color=FG_DIM), range=[0, 100], fixedrange=False, row=2, col=1)
    fig.update_yaxes(title_text="MACD",      title_font=dict(size=10, color=FG_DIM), autorange=True, fixedrange=False, row=3, col=1)
    fig.update_yaxes(title_text="Portfolio", title_font=dict(size=10, color=FG_DIM), autorange=True, fixedrange=False, row=4, col=1)

    # Single consolidated update — range + tick labels + axis link, all in one dict
    # so nothing overwrites the range that was set above
    shared_x = dict(
        matches="x",
        showticklabels=True,
        tickformat="%b '%y",
        tickfont=dict(size=9, color=FG_DIM),
        tickangle=0,
        gridcolor=BG_BORDER,
        zerolinecolor=BG_BORDER,
        showspikes=True,
        spikecolor=FG_DIM,
        spikethickness=1,
        rangeslider=dict(visible=False),
    )
    fig.update_layout(
        xaxis =dict(**shared_x, range=[view_start, view_end]),
        xaxis2=shared_x,
        xaxis3=shared_x,
        xaxis4=shared_x,
    )

    # Subplot title styling
    for ann in fig.layout.annotations:
        ann.font.update(size=11, color=FG_DIM)

    chart_html = fig.to_html(
        full_html=False,
        include_plotlyjs="cdn",
        config={
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d"],
            "scrollZoom": True,
        },
    )

    stats = {
        "ticker":     ticker,
        "price":      current,
        "hv_rank":    hvr["hv_rank"],
        "current_hv": hvr["current_hv"],
        "adx":        adx_val,
        "rsi":        rsi_val,
        "mtf_score":  mtf_score,
        "daily":      daily_reg,
        "weekly":     weekly_reg,
        "monthly":    monthly_reg,
        "entry":      entry_sig,
        "forecast":   fsum,
        "metrics":    metrics,
        "kelly":      kelly_pct,
    }

    return chart_html, stats
