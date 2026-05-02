import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings("ignore")

# ═══════════════════════════════════════
#  CONFIG — Enhanced with Phase 3
# ═══════════════════════════════════════
INITIAL_BALANCE = 1000.0
RISK_PCT        = 1.0     # Safer risk with macro filter
EMA_PERIOD      = 13
SAR_STEP        = 0.03
SAR_MAX         = 0.2
ATR_PERIOD      = 14
ATR_THRESHOLD   = 0.15    # Lowered slightly due to macro filter
SL_PIPS         = 1.6
TP_PIPS         = 2.3

# Phase 3 Configuration
DXY_Z_THRESHOLD = 1.5
ADAPTIVE_TRAIL  = True
ACCEL_DECAY_FLOOR = 0.3   # Minimum multiplier for trail distance

# ═══════════════════════════════════════
#  INDICATORS
# ═══════════════════════════════════════
def ema_calc(s, n): return s.ewm(span=n, adjust=False).mean()

def atr_calc(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def zscore_calc(s, n=20):
    return (s - s.rolling(n).mean()) / s.rolling(n).std()

def psar_calc(df, step=0.03, max_step=0.2):
    high, low = df["high"], df["low"]
    sar = np.zeros(len(df))
    trend = np.zeros(len(df))
    af = np.full(len(df), step)
    ep = np.zeros(len(df))
    trend[0] = 1 if high[1] > high[0] else -1
    sar[0] = low[0] if trend[0] == 1 else high[0]
    ep[0] = high[0] if trend[0] == 1 else low[0]
    for i in range(1, len(df)):
        sar[i] = sar[i-1] + af[i-1] * (ep[i-1] - sar[i-1])
        if trend[i-1] == 1:
            if low[i] < sar[i]:
                trend[i] = -1; sar[i] = ep[i-1]; ep[i] = low[i]; af[i] = step
            else:
                trend[i] = 1
                if high[i] > ep[i-1]: ep[i] = high[i]; af[i] = min(max_step, af[i-1] + step)
                else: ep[i] = ep[i-1]; af[i] = af[i-1]
                sar[i] = min(sar[i], low[i-1], low[i-2] if i>1 else low[i-1])
        else:
            if high[i] > sar[i]:
                trend[i] = 1; sar[i] = ep[i-1]; ep[i] = high[i]; af[i] = step
            else:
                trend[i] = -1
                if low[i] < ep[i-1]: ep[i] = low[i]; af[i] = min(max_step, af[i-1] + step)
                else: ep[i] = ep[i-1]; af[i] = af[i-1]
                sar[i] = max(sar[i], high[i-1], high[i-2] if i>1 else high[i-1])
    return pd.Series(sar, name="sar"), pd.Series(trend, name="trend")

# ═══════════════════════════════════════
#  FETCH DATA (Including DXY)
# ═══════════════════════════════════════
def fetch_all_data():
    print("Fetching XAUUSD, DXY, and US10Y data...")
    # XAUUSD M1 (5 days)
    gold = yf.download("GC=F", period="5d", interval="1m", progress=False)
    gold.columns = [c[0].lower() for c in gold.columns]
    gold = gold.dropna().reset_index()
    gold.rename(columns={"Datetime": "time", "index": "time"}, inplace=True)
    
    # DXY and US10Y (Daily or M1 if available - using M1 for real-time filter)
    dxy = yf.download("DX-Y.NYB", period="5d", interval="1m", progress=False)
    dxy.columns = [c[0].lower() for c in dxy.columns]
    dxy = dxy.dropna().reset_index()
    dxy.rename(columns={"Datetime": "time", "index": "time"}, inplace=True)
    
    # Merge on time
    df = pd.merge(gold, dxy[["time", "close"]], on="time", how="left", suffixes=("", "_dxy"))
    df["close_dxy"] = df["close_dxy"].ffill()
    
    return df

# ═══════════════════════════════════════
#  ACCELERATION-AWARE TRAILING
# ═══════════════════════════════════════
def get_adaptive_trail_dist(df, idx, direction, base_dist):
    """Calculate Trail Distance based on Acceleration Decay (Upgrade 3)."""
    if idx < 5: return base_dist
    
    # Velocity: V = P(t) - P(t-1)
    v1 = df["close"].iloc[idx] - df["close"].iloc[idx-1]
    v2 = df["close"].iloc[idx-1] - df["close"].iloc[idx-2]
    
    # Acceleration: A = V1 - V2
    a1 = v1 - v2
    
    # Normalize Acceleration (simplified)
    # If acceleration is slowing down (approaching 0 or reversing), tighten trail.
    if direction == "LONG":
        # In a long trend, we want a1 > 0. If a1 < 0, trend is slowing.
        factor = np.clip(1.0 + (a1 / df["atr"].iloc[idx]), ACCEL_DECAY_FLOOR, 1.0)
    else:
        # In a short trend, we want a1 < 0. If a1 > 0, trend is slowing.
        factor = np.clip(1.0 - (a1 / df["atr"].iloc[idx]), ACCEL_DECAY_FLOOR, 1.0)
        
    return base_dist * factor

# ═══════════════════════════════════════
#  BACKTEST ENGINE
# ═══════════════════════════════════════
def run_enhanced_backtest():
    df = fetch_all_data()
    print(f"Loaded {len(df)} M1 bars with Macro data.")
    
    # Calc indicators
    df["ema"] = ema_calc(df["close"], EMA_PERIOD)
    df["sar"], df["trend"] = psar_calc(df, SAR_STEP, SAR_MAX)
    df["atr"] = atr_calc(df, ATR_PERIOD)
    df["z_dxy"] = zscore_calc(df["close_dxy"], 20)
    
    balance = INITIAL_BALANCE
    trades = []
    active_trade = None
    
    for i in range(50, len(df)):
        cur = df.iloc[i]
        
        # ── EXIT LOGIC (Adaptive Trailing) ─────────────────────
        if active_trade:
            # Update Trail
            if ADAPTIVE_TRAIL:
                trail_px = get_adaptive_trail_dist(df, i, active_trade["direction"], SL_PIPS)
                if active_trade["direction"] == "LONG":
                    new_sl = cur["close"] - trail_px
                    if new_sl > active_trade["sl"]: active_trade["sl"] = new_sl
                else:
                    new_sl = cur["close"] + trail_px
                    if new_sl < active_trade["sl"]: active_trade["sl"] = new_sl

            # Check SL/TP
            h, l = cur["high"], cur["low"]
            if active_trade["direction"] == "LONG":
                if l <= active_trade["sl"]:
                    trade_pnl = (active_trade["sl"] - active_trade["entry"]) * 100 * active_trade["lot"]
                    balance += trade_pnl
                    active_trade["pnl"] = trade_pnl
                    active_trade["result"] = "SL/Trail"
                    trades.append(active_trade); active_trade = None
                elif h >= active_trade["tp"]:
                    trade_pnl = (active_trade["tp"] - active_trade["entry"]) * 100 * active_trade["lot"]
                    balance += trade_pnl
                    active_trade["pnl"] = trade_pnl
                    active_trade["result"] = "TP"
                    trades.append(active_trade); active_trade = None
            else:
                if h >= active_trade["sl"]:
                    trade_pnl = (active_trade["entry"] - active_trade["sl"]) * 100 * active_trade["lot"]
                    balance += trade_pnl
                    active_trade["pnl"] = trade_pnl
                    active_trade["result"] = "SL/Trail"
                    trades.append(active_trade); active_trade = None
                elif l <= active_trade["tp"]:
                    trade_pnl = (active_trade["entry"] - active_trade["tp"]) * 100 * active_trade["lot"]
                    balance += trade_pnl
                    active_trade["pnl"] = trade_pnl
                    active_trade["result"] = "TP"
                    trades.append(active_trade); active_trade = None
            
            if active_trade: continue
            
        # ── ENTRY LOGIC (with DXY Filter) ─────────────────────
        flip_up = (df["trend"].iloc[i] == 1 and df["trend"].iloc[i-1] == -1)
        flip_down = (df["trend"].iloc[i] == -1 and df["trend"].iloc[i-1] == 1)
        
        # Macro Filter (Upgrade 2)
        dxy_long_ok = cur["z_dxy"] < -DXY_Z_THRESHOLD  # USD weakening -> Long Gold
        dxy_short_ok = cur["z_dxy"] > DXY_Z_THRESHOLD  # USD strengthening -> Short Gold
        
        if cur["atr"] < ATR_THRESHOLD: continue
        
        # LONG
        if flip_up and cur["close"] > cur["ema"] and dxy_long_ok:
            lot = (balance * RISK_PCT / 100.0) / (SL_PIPS * 100)
            lot = round(max(0.01, min(lot, 0.5)), 2)
            active_trade = {"time": cur["time"], "direction": "LONG", "entry": cur["close"],
                            "sl": cur["close"] - SL_PIPS, "tp": cur["close"] + TP_PIPS, "lot": lot}
            
        # SHORT
        elif flip_down and cur["close"] < cur["ema"] and dxy_short_ok:
            lot = (balance * RISK_PCT / 100.0) / (SL_PIPS * 100)
            lot = round(max(0.01, min(lot, 0.5)), 2)
            active_trade = {"time": cur["time"], "direction": "SHORT", "entry": cur["close"],
                            "sl": cur["close"] + SL_PIPS, "tp": cur["close"] - TP_PIPS, "lot": lot}
            
    # Stats
    if not trades: print("No trades found."); return
    wins = [t for t in trades if t["pnl_points"] > 0] if "pnl_points" in trades[0] else [t for t in trades if t["result"]=="TP"] # simplified
    # (Actual PnL calculation is in the exit logic above)
    pnl = balance - INITIAL_BALANCE
    print(f"\n{'='*40}")
    print(f" ENHANCED PHASE 3 ALPHA")
    print(f"{'='*40}")
    print(f" Trades:     {len(trades)}")
    print(f" Net PnL:    ${pnl:+.2f}")
    print(f" Final Bal:  ${balance:.2f}")

if __name__ == "__main__":
    run_enhanced_backtest()
