"""
XAUUSD PDH/PDL Strategy Bot — pdhl_bot.py
Runs independently from bot_free.py on port 8081 / EA port 9998

Strategy:
  REVERSAL: Price sweeps PDH/PDL → rejection candle + MSS on M5 → enter counter-trend
  BREAKOUT: Strong close beyond PDH/PDL → wait for retest → enter with trend

Quality filters: HTF bias, session, RSI, one trade per level per day
"""
import socket, threading, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
import requests as req_lib
import pandas as pd
import numpy as np

# ═══════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════
WEBHOOK_PORT   = 8081        # different from bot_free.py (8080)
WEBHOOK_SECRET = "piyush_pdh_bot_2026"
EA_PORT        = 9998        # different from XAU_Bot (9999)
SL_ATR_MULT    = 1.0
TP1_RR         = 1.5
TP2_RR         = 3.0
LOOP_S         = 45
TG_TOKEN       = ""
TG_CHAT_ID     = ""
MAX_DAILY_LOSS_PCT = 3.0  # Safety: stop trading if daily DD exceeds 3%
BASE_DIR       = Path(__file__).resolve().parent
RESULTS_DIR    = BASE_DIR / "results"
PDH_TRADE_LOG  = RESULTS_DIR / "pdh_trade_log.csv"

PROXIMITY_PCT  = 0.0015   # 0.15% of price = ~$7.5 on $5000 gold
BREAKOUT_BARS  = 3        # candles to wait for retest after breakout

# ═══════════════════════════════════════
#  GLOBALS
# ═══════════════════════════════════════
app             = Flask(__name__)
pending_command = None
command_lock    = threading.Lock()
ea_status_cache = {}
ea_connected    = False
signal_log      = []
tapped_today    = {"PDH": False, "PDL": False}  # one trade per level per day
last_reset_day  = None
daily_start_balance = None
daily_start_date    = None
daily_loss_halt     = False

# ═══════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════
def tg(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        req_lib.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                     data={"chat_id": TG_CHAT_ID, "text": f"🏛 PDH Bot\n{msg}"}, timeout=5)
    except: pass

import csv, os
def log_trade(event, signal="", entry="", sl="", tp2="", lot="", result="", balance="", setup=""):
    os.makedirs(PDH_TRADE_LOG.parent, exist_ok=True)
    file_exists = os.path.exists(PDH_TRADE_LOG)
    try:
        with open(PDH_TRADE_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if not file_exists:
                w.writerow(["timestamp","event","signal","entry","sl","tp2","lot","result","balance","setup"])
            w.writerow([datetime.now().isoformat(), event, signal, entry, sl, tp2, lot, result, balance, setup])
    except Exception as e:
        print(f"[LOG] CSV error: {e}")

def check_daily_loss():
    global daily_start_balance, daily_start_date, daily_loss_halt
    today = datetime.now().date()
    bal = float(ea_status_cache.get("balance", 0))
    eq  = float(ea_status_cache.get("equity", 0))
    if bal <= 0: return False
    if daily_start_date != today:
        daily_start_balance = bal
        daily_start_date = today
        daily_loss_halt = False
        print(f"[SAFETY] New day — daily start balance: ${bal:.2f}")
    if daily_start_balance and eq > 0:
        dd_pct = (1 - eq / daily_start_balance) * 100
        if dd_pct >= MAX_DAILY_LOSS_PCT:
            if not daily_loss_halt:
                daily_loss_halt = True
                tg(f"🛑 DAILY LOSS LIMIT — {dd_pct:.1f}% DD. PDH Bot halted.")
            return True
    return False

def fetch_candles(interval="5m", period="5d"):
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
               f"?interval={interval}&range={period}&includePrePost=false")
        r = req_lib.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        df = pd.DataFrame({
            "time":  pd.to_datetime(res["timestamp"], unit="s", utc=True),
            "open":  q["open"], "high": q["high"],
            "low":   q["low"],  "close": q["close"],
        }).dropna().reset_index(drop=True)
        return df
    except Exception as e:
        print(f"[DATA] {e}"); return None

def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def atr_calc(df, n=14):
    h,l,c = df["high"],df["low"],df["close"]
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()
def rsi_calc(s, n=14):
    d=s.diff(); g=d.clip(lower=0).rolling(n).mean()
    l=(-d.clip(upper=0)).rolling(n).mean()
    return 100-(100/(1+g/l.replace(0,np.nan)))

def in_session(dt_utc):
    h = dt_utc.hour
    # Filter toxic hours identified in recent backtests
    if h in (1, 9, 10):
        return False
    return (6 <= h < 12) or (12 <= h < 20)

def get_htf_bias():
    df = fetch_candles("1h", "60d")
    if df is None or len(df) < 200: return None
    e50 = ema(df["close"],50).iloc[-1]
    e200= ema(df["close"],200).iloc[-1]
    return "BULL" if e50 > e200 else "BEAR"

# ═══════════════════════════════════════
#  CORE: GET PREVIOUS DAY HIGH/LOW
# ═══════════════════════════════════════
def get_pdh_pdl():
    """Returns (pdh, pdl) from yesterday's daily candle."""
    try:
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
               "?interval=1d&range=5d&includePrePost=false")
        r = req_lib.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=10)
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        df = pd.DataFrame({
            "time":  pd.to_datetime(res["timestamp"], unit="s", utc=True),
            "high":  q["high"], "low": q["low"], "close": q["close"]
        }).dropna().reset_index(drop=True)
        if len(df) < 2: return None, None
        # Yesterday = second to last row (last row = today incomplete)
        pdh = df["high"].iloc[-2]
        pdl = df["low"].iloc[-2]
        return round(pdh, 2), round(pdl, 2)
    except Exception as e:
        print(f"[PDH] fetch error: {e}"); return None, None

# ═══════════════════════════════════════
#  SIGNAL ENGINE
# ═══════════════════════════════════════
def detect_pdh_signal():
    global tapped_today, last_reset_day

    # Reset tapped_today at start of new day
    today = datetime.now(timezone.utc).date()
    if last_reset_day != today:
        tapped_today = {"PDH": False, "PDL": False}
        last_reset_day = today
        print(f"[PDH] New day — levels reset")

    pdh, pdl = get_pdh_pdl()
    if pdh is None: return None, None, None, "Could not fetch PDH/PDL"

    df = fetch_candles("5m", "3d")
    if df is None or len(df) < 30: return None, None, None, "Not enough M5 data"

    now_utc = df["time"].iloc[-1].to_pydatetime()
    if not in_session(now_utc):
        return None, None, None, f"Outside session (UTC {now_utc.hour}h)"

    df    = df.tail(50).reset_index(drop=True)
    atr_s = atr_calc(df)
    rsi_s = rsi_calc(df["close"])
    atr_v = atr_s.iloc[-1]
    rsi_v = rsi_s.iloc[-1]
    cur   = df["close"].iloc[-1]

    if pd.isna(atr_v): return None, None, None, "ATR not ready"
    if rsi_v > 78 or rsi_v < 22: return None, None, None, f"RSI extreme ({rsi_v:.0f})"

    bias  = get_htf_bias()
    prox  = cur * PROXIMITY_PCT

    n = len(df)

    # ── CHECK PDH ──────────────────────────────────────────────
    if not tapped_today["PDH"] and abs(cur - pdh) < prox * 2:
        # REVERSAL at PDH: DISABLED — backtest shows 30% WR, negative P&L
        # Only BREAKOUT_PDH is profitable (57% WR, +$36)
        last_high = df["high"].iloc[-1]
        last_close= df["close"].iloc[-1]
        prev_close= df["close"].iloc[-2]
        wick_above = last_high > pdh + (prox * 0.3)
        close_below= last_close < pdh
        mss = df["close"].iloc[-2] > df["open"].iloc[-2] and last_close < df["open"].iloc[-1]

        if wick_above and close_below and mss and (bias == "BEAR" or bias is None):
            tapped_today["PDH"] = True
            # SWEEP_REV_PDH disabled — 30% WR in backtest
            print(f"[PDH] SWEEP_REV_PDH skipped (30% WR, disabled by backtest)")
            return None, None, None, f"SWEEP_REV_PDH disabled"

        # BREAKOUT above PDH: strong close above, wait for retest
        if last_close > pdh + prox and prev_close < pdh:
            # Check: did price come back to retest PDH from above?
            retest = any(
                pdh - prox <= df["low"].iloc[-(i+1)] <= pdh + prox and
                df["close"].iloc[-(i+1)] > pdh
                for i in range(1, min(BREAKOUT_BARS+1, n))
            )
            if retest and (bias == "BULL" or bias is None):
                tapped_today["PDH"] = True
                reason = f"BREAKOUT@PDH {pdh:.2f} retest | close={last_close:.2f} | RSI={rsi_v:.0f} | bias={bias}"
                return "LONG", atr_v, "BREAKOUT", reason

    # ── CHECK PDL ──────────────────────────────────────────────
    if not tapped_today["PDL"] and abs(cur - pdl) < prox * 2:
        last_low  = df["low"].iloc[-1]
        last_close= df["close"].iloc[-1]
        prev_close= df["close"].iloc[-2]
        wick_below = last_low < pdl - (prox * 0.3)
        close_above= last_close > pdl
        mss = df["close"].iloc[-2] < df["open"].iloc[-2] and last_close > df["open"].iloc[-1]

        if wick_below and close_above and mss and (bias == "BULL" or bias is None):
            tapped_today["PDL"] = True
            # SWEEP_REV_PDL disabled — 57% WR but -$47 net P&L (losses too large)
            print(f"[PDL] SWEEP_REV_PDL skipped (negative P&L, disabled by backtest)")
            return None, None, None, f"SWEEP_REV_PDL disabled"

        # BREAKOUT below PDL
        if last_close < pdl - prox and prev_close > pdl:
            retest = any(
                pdl - prox <= df["high"].iloc[-(i+1)] <= pdl + prox and
                df["close"].iloc[-(i+1)] < pdl
                for i in range(1, min(BREAKOUT_BARS+1, n))
            )
            if retest and (bias == "BEAR" or bias is None):
                tapped_today["PDL"] = True
                reason = f"BREAKOUT@PDL {pdl:.2f} retest | close={last_close:.2f} | RSI={rsi_v:.0f} | bias={bias}"
                return "SHORT", atr_v, "BREAKOUT", reason

    return None, None, None, f"No setup | cur={cur:.2f} PDH={pdh:.2f} PDL={pdl:.2f} | bias={bias}"

# ═══════════════════════════════════════
#  SIGNAL LOOP
# ═══════════════════════════════════════
def signal_loop():
    global pending_command
    print("[PDH] Signal engine started — checking every 45s")
    time.sleep(10)
    while True:
        try:
            now = datetime.now().strftime("%H:%M:%S")
            if not ea_connected:
                print(f"[{now}] Waiting for PDH EA..."); time.sleep(LOOP_S); continue
            if ea_status_cache.get("positions","0") != "0":
                print(f"[{now}] In trade — waiting"); time.sleep(LOOP_S); continue
            with command_lock:
                if pending_command: time.sleep(LOOP_S); continue

            print(f"[{now}] Scanning PDH/PDL levels...")
            if check_daily_loss():
                print(f"[{now}] 🛑 Daily loss limit — halted"); time.sleep(LOOP_S); continue
            signal, atr_v, setup_type, reason = detect_pdh_signal()
            signal_log.append({"time":now,"signal":signal or "NONE","reason":reason})
            if len(signal_log) > 30: signal_log.pop(0)

            if signal and atr_v and setup_type:
                sl  = atr_v * SL_ATR_MULT
                tp1 = sl * TP1_RR
                tp2 = sl * TP2_RR
                cmd = f"{signal}|{sl:.2f}|{tp1:.2f}|{tp2:.2f}|{setup_type}"
                with command_lock: pending_command = cmd
                emoji = "🔄" if setup_type == "REVERSAL" else "🚀"
                tg(f"{emoji} PDH {setup_type}: {signal}\n{reason}\nSL:{sl:.1f} TP1:{tp1:.1f} TP2:{tp2:.1f}")
                print(f"[PDH] ✅ {setup_type} {signal} queued — {reason}")
            else:
                print(f"[PDH] {reason}")
        except Exception as e:
            print(f"[PDH ERROR] {e}")
        time.sleep(LOOP_S)

# ═══════════════════════════════════════
#  TCP SERVER (same pattern as bot_free)
# ═══════════════════════════════════════
def tcp_server():
    global ea_connected, ea_status_cache, pending_command
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", EA_PORT)); s.listen(5)
    print(f"[TCP] PDH Bot listening on port {EA_PORT}...")
    while True:
        try:
            conn, _ = s.accept()
            threading.Thread(target=handle_ea, args=(conn,), daemon=True).start()
        except Exception as e: print(f"[TCP] {e}")

def handle_ea(conn):
    global ea_connected, ea_status_cache, pending_command
    try:
        conn.settimeout(5); data = b""
        while True:
            chunk = conn.recv(1024)
            if not chunk: break
            data += chunk
            if b"\n" in data: break
        msg = data.decode("utf-8").strip()
        if not msg: conn.close(); return
        ea_connected = True
        
        # 1. Update Cache
        if msg.startswith("STATUS|"):
            for pair in msg[7:].split(","):
                if "=" in pair:
                    k,v = pair.split("=",1)
                    ea_status_cache[k.strip()] = v.strip()
            ea_status_cache["last_seen"] = datetime.now().strftime("%H:%M:%S")
        elif msg.startswith("OPENED|"):
            p = dict(x.split("=") for x in msg[7:].split("|") if "=" in x)
            tg(f"✅ TRADE OPENED\nTicket:{p.get('ticket','?')} Entry:{p.get('entry','?')}\nType:{p.get('type','?')} SL:{p.get('sl','?')} TP2:{p.get('tp2','?')}")
            log_trade("OPEN", signal=p.get('ticket',''), entry=p.get('entry',''),
                      sl=p.get('sl',''), tp2=p.get('tp2',''), lot=p.get('lot',''),
                      balance=ea_status_cache.get('balance',''), setup=p.get('type',''))
        elif msg.startswith("PARTIAL_CLOSE|"):
            p = dict(x.split("=") for x in msg[14:].split("|") if "=" in x)
            tg(f"💰 PARTIAL CLOSE 50%\nTicket:{p.get('ticket','?')} +{p.get('profit_pips','?')}pips")
        elif msg.startswith("TRADE_CLOSED|"):
            p = dict(x.split("=") for x in msg[13:].split("|") if "=" in x)
            bal = float(ea_status_cache.get("balance", 0))
            eq  = float(ea_status_cache.get("equity", 0))
            result = "WIN" if eq >= bal else "LOSS"
            icon = "✅ WIN" if result == "WIN" else "❌ LOSS"
            tg(f"🔴 CLOSED {icon}\nTicket:{p.get('ticket','?')}")
            log_trade("CLOSE", result=result, balance=str(bal))
        elif msg.startswith("ERROR|"):
            tg(f"⚠️ {msg}")

        # 2. Reliable Command Dispatch
        # We only clear pending_command IF the socket successfully sends the data.
        # This prevents signals from being dropped during network timeouts.
        with command_lock:
            cmd = pending_command if pending_command else "NONE"
        
        try:
            conn.sendall((cmd+"\n").encode("utf-8"))
            if cmd != "NONE":
                with command_lock:
                    pending_command = None # SUCCESS: Clear it now
                    print(f"[TCP] Command '{cmd}' delivered and cleared.")
        except Exception as send_err:
            print(f"[TCP ERROR] Failed to deliver command: {send_err}")
            # We DON'T clear pending_command here, so it retries on the next heartbeat.

    except Exception as e: print(f"[EA] {e}")
    finally: conn.close()

# ═══════════════════════════════════════
#  FLASK
# ═══════════════════════════════════════
@app.route("/webhook", methods=["POST"])
def webhook():
    global pending_command
    try:
        data = request.get_json(force=True)
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error":"unauthorized"}), 401
        signal = str(data.get("signal","")).upper()
        if signal == "CLOSE":
            with command_lock: pending_command = "CLOSE"
            return jsonify({"status":"close_queued"}), 200
        if signal not in ("LONG","SHORT"):
            return jsonify({"error":f"unknown: {signal}"}), 400
        sl = float(data.get("sl",5.0))
        setup = data.get("type","MANUAL")
        cmd = f"{signal}|{sl:.2f}|{sl*TP1_RR:.2f}|{sl*TP2_RR:.2f}|{setup}"
        with command_lock: pending_command = cmd
        return jsonify({"status":"queued","command":cmd}), 200
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/status", methods=["GET"])
def status():
    pdh, pdl = get_pdh_pdl()
    return jsonify({"status":"running","ea_connected":ea_connected,
                    "ea":ea_status_cache,"pending":pending_command,
                    "pdh":pdh,"pdl":pdl,"tapped_today":tapped_today,
                    "last_signal":signal_log[-1] if signal_log else None,
                    "time":datetime.now().isoformat()})

@app.route("/log", methods=["GET"])
def log(): return jsonify({"signal_log":signal_log})

# ═══════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════
if __name__ == "__main__":
    print("="*55)
    print("  XAUUSD PDH/PDL Strategy Bot")
    print("="*55)
    print("  Reversal: sweep PDH/PDL → MSS → enter counter-trend")
    print("  Breakout: strong close → retest → enter with trend")
    print("  One trade per level per day (quality filter)")
    print(f"  EA port : {EA_PORT}  |  Webhook: {WEBHOOK_PORT}")
    print(f"  Status  : http://localhost:{WEBHOOK_PORT}/status")
    print(f"  Log     : http://localhost:{WEBHOOK_PORT}/log")
    print("="*55)
    threading.Thread(target=tcp_server, daemon=True).start()
    threading.Thread(target=signal_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=WEBHOOK_PORT, debug=False, use_reloader=False)
