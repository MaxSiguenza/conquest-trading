import numpy as np
import pandas as pd

from config import IndicatorConfig
from indicators.macd import calculate_macd
from indicators.moving_averages import add_moving_averages
from indicators.rsi import calculate_rsi
from indicators.timeframes import add_weekly_signals, add_monthly_signals
from indicators.volatility import calculate_atr, calculate_adx, calculate_bollinger_bands, calculate_obv


def generate_signals(
    df: pd.DataFrame,
    cfg: IndicatorConfig,
    vix: pd.Series = None,
    earnings_dates: pd.DatetimeIndex = None,
) -> pd.DataFrame:
    """
    Three-layer signal architecture:

    LAYER 1 — MONTHLY REGIME (macro backdrop)
      Monthly 6-SMA > 12-SMA = major uptrend confirmed
      Changes only a few times per year. When this is bearish, stay out
      no matter what the daily chart says.

    LAYER 2 — WEEKLY REGIME (intermediate trend)
      Weekly 10-SMA > 20-SMA = intermediate uptrend confirmed
      Changes several times per year. Filters out daily noise.

    LAYER 3 — DAILY REGIME + ENTRY TIMING
      Daily golden cross (SMA 50 > 200) = current uptrend active
      MACD recovering + RSI not extended = good entry point within the trend

    MTF SCORE = monthly + weekly + daily regime (0–3)
      3/3 = all timeframes aligned bullish → highest conviction entry
      2/3 = most timeframes agree → can still enter with smaller size
      1/3 or 0/3 = conflicted or bearish → stay flat

    EXIT: only when daily regime dies (death cross) — we don't exit
    because weekly or monthly briefly dip. The daily SMA 50/200 is
    slow enough to absorb that noise.
    """
    # ── Daily indicators ───────────────────────────────────────────────
    df = add_moving_averages(df, cfg.sma_short, cfg.sma_long)
    df["RSI"] = calculate_rsi(df["Close"], cfg.rsi_period)
    macd_df = calculate_macd(df["Close"], cfg.macd_short, cfg.macd_long, cfg.macd_signal)
    df = pd.concat([df, macd_df], axis=1)
    df["ATR"]     = calculate_atr(df)
    df["Vol_Avg"] = df["Volume"].rolling(20).mean()
    df["Vol_OK"]  = df["Volume"] >= df["Vol_Avg"] * 1.1

    # ADX — trend strength. Filters weak golden crosses.
    adx_df = calculate_adx(df)
    df = pd.concat([df, adx_df], axis=1)

    # Bollinger Bands — squeeze detects coiling before breakout
    bb_df = calculate_bollinger_bands(df)
    df = pd.concat([df, bb_df], axis=1)

    # OBV — institutional money flow confirmation
    df["OBV"]       = calculate_obv(df)
    df["OBV_MA"]    = df["OBV"].rolling(20).mean()
    df["OBV_Trend"] = df["OBV"] > df["OBV_MA"]   # True = net accumulation

    # ── TTM Squeeze (pandas-ta) ──────────────────────────────────────
    # Detects low-volatility coiling (BB inside KC) before a breakout.
    # SQZ_ON=1 means the spring is loaded. SQZ_FIRED means it just released.
    try:
        import pandas_ta as pta
        sqz_df = pta.squeeze(df["High"], df["Low"], df["Close"],
                             bb_length=20, kc_length=20, detailed=False)
        if sqz_df is not None and not sqz_df.empty:
            # Momentum histogram column (named like SQZ_20_2.0_20_1.5)
            mom_cols = [c for c in sqz_df.columns
                        if c.startswith("SQZ_") and c not in ("SQZ_ON", "SQZ_OFF", "NO_SQZ")]
            if mom_cols:
                df["SQZ_MOMENTUM"] = sqz_df[mom_cols[0]].reindex(df.index)
            if "SQZ_ON" in sqz_df.columns:
                df["SQZ_ON"]  = sqz_df["SQZ_ON"].reindex(df.index).fillna(0).astype(int)
            if "SQZ_OFF" in sqz_df.columns:
                df["SQZ_OFF"] = sqz_df["SQZ_OFF"].reindex(df.index).fillna(0).astype(int)
            # Squeeze FIRED = was active last bar, now released → potential breakout
            if "SQZ_ON" in df.columns:
                sqz_was_on   = df["SQZ_ON"].shift(1, fill_value=0)
                df["SQZ_FIRED"] = ((sqz_was_on == 1) & (df["SQZ_ON"] == 0)).astype(int)
    except Exception:
        pass  # pandas-ta squeeze is optional — system works without it

    # 52-week high proximity — stocks near highs have momentum
    df["High_52w"]     = df["Close"].rolling(252).max()
    df["Near_52w_High"] = df["Close"] >= df["High_52w"] * 0.95

    # ── Weekly + monthly indicators ────────────────────────────────────
    df = add_weekly_signals(df)
    df = add_monthly_signals(df)

    # ── Daily regime (golden / death cross) ───────────────────────────
    golden_cross = (
        df["SMA_Short"].notna() &
        df["SMA_Long"].notna() &
        (df["SMA_Short"] > df["SMA_Long"])
    )
    df["Regime"] = golden_cross.astype(int)

    # ── Multi-timeframe conviction score ──────────────────────────────
    w_regime = df.get("W_Regime", pd.Series(1, index=df.index)).fillna(0).astype(int)
    m_regime = df.get("M_Regime", pd.Series(1, index=df.index)).fillna(0).astype(int)

    df["MTF_Score"] = df["Regime"] + w_regime + m_regime  # 0–3

    # ── External filter: VIX regime ────────────────────────────────────
    if vix is not None and not vix.empty:
        df = df.join(vix, how="left")
        df["VIX"] = df["VIX"].ffill()

    # ── External filter: earnings blackout ─────────────────────────────
    in_blackout = pd.Series(False, index=df.index)
    if earnings_dates is not None and len(earnings_dates) > 0:
        blackout_days = set()
        for ed in earnings_dates:
            for offset in range(-cfg.earnings_buffer_days, 2):
                blackout_days.add(ed + pd.Timedelta(days=offset))
        in_blackout = df.index.normalize().isin(blackout_days)

    vix_ok = (
        df["VIX"] <= cfg.vix_threshold
        if "VIX" in df.columns
        else pd.Series(True, index=df.index)
    )

    # ── Entry signal ───────────────────────────────────────────────────
    # Require at least 2/3 timeframes bullish + entry conditions + filters
    vol_ok  = df.get("Vol_OK",   pd.Series(True,  index=df.index)).fillna(True)
    obv_ok  = df.get("OBV_Trend", pd.Series(True, index=df.index)).fillna(True)
    adx_ok  = (df["ADX"] >= 20) if "ADX" in df.columns else pd.Series(True, index=df.index)

    # ── MACD crossover events (bullish cross = line crosses above signal) ──
    macd_bull = df["MACD_Line"] > df["MACD_Signal"]
    macd_prev = macd_bull.shift(1, fill_value=False)

    # True only on the day MACD turns bullish, held open for 5 trading days
    macd_cross_day = macd_bull & ~macd_prev
    macd_window    = macd_cross_day.rolling(5, min_periods=1).max().astype(bool)

    # Bearish crossover for chart markers
    df["MACD_Cross_Up"]   = (macd_cross_day & (df["MTF_Score"] >= 2)).astype(int)
    df["MACD_Cross_Down"] = (~macd_bull & macd_prev).astype(int)

    # ── RSI pullback-recovery entry ────────────────────────────────────────
    # RSI dipped below 45 recently and is recovering upward in a bull trend
    rsi_dip   = df["RSI"].rolling(10, min_periods=1).min() < 45
    rsi_recov = (df["RSI"] > df["RSI"].shift(1)) & (df["RSI"] < cfg.rsi_overbought)
    rsi_bounce = rsi_dip & rsi_recov & (df["RSI"] > 35)

    # Squeeze fired is an additional entry condition — stock just broke out of coil
    sqz_fired = df.get("SQZ_FIRED", pd.Series(0, index=df.index)).fillna(0).astype(bool)

    entry = (
        (df["MTF_Score"] >= 2) &                              # multi-timeframe agreement
        (df["Regime"] == 1) &                                  # daily must be bullish
        (macd_window | rsi_bounce | sqz_fired) &              # momentum shift, dip, or squeeze break
        (df["RSI"] < cfg.rsi_overbought) &                     # not entering at a peak
        vix_ok &                                               # market calm
        obv_ok &                                               # institutions are accumulating
        adx_ok                                                 # trend is strong enough to trade
    )

    df["Entry_Signal"] = entry.astype(int)
    df["Position"]     = df["Entry_Signal"]

    return df
