#!/usr/bin/env python3
"""
NIFTY EMA5/10 Flip -- Telegram Alert Bot (30-Min Status Updates & Full Status on All Alerts)
===========================================================================================
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

IST = timezone(timedelta(hours=5, minutes=30))
STATE_PATH = os.environ.get("STATE_PATH", "state.json")

TELEGRAM_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID")
PTS_THRESHOLD = float(os.environ.get("PTS_THRESHOLD", "100"))
MOVE_STEP = 50.0   # every additional 50pt milestone from flip/open gets an alert

# 30-minute interval checkpoints from 09:45 to 15:15 (09:15 is covered by Market Open)
INTERVAL_TIMES = [
    "09:45", "10:15", "10:45", "11:15", "11:45", 
    "12:15", "12:45", "13:15", "13:45", "14:15", "14:45", "15:15"
]

K5 = 2 / 6     # 5-period EMA smoothing constant
K10 = 2 / 11   # 10-period EMA smoothing constant


# =====================================================================
# Telegram
# =====================================================================
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("BOT_TOKEN / CHAT_ID not set -- skipping send:", text, file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)
    print("SENT:", text)


# =====================================================================
# Free/unofficial NSE quote fetch
# =====================================================================
def fetch_nifty_quote():
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    }
    session = requests.Session()
    session.headers.update(headers)
    session.get("https://www.nseindia.com", timeout=10)
    time.sleep(1)
    resp = session.get("https://www.nseindia.com/api/allIndices", timeout=10)
    resp.raise_for_status()
    data = resp.json()

    rows = data.get("data", data if isinstance(data, list) else [])
    row = None
    for d in rows:
        label = str(d.get("index") or d.get("indexName") or d.get("index_name") or "").strip().upper()
        if label == "NIFTY 50":
            row = d
            break
    if row is None:
        seen = [d.get("index") or d.get("indexName") for d in rows][:10]
        raise RuntimeError(f"'NIFTY 50' not found in allIndices response. Seen: {seen}")

    def pick(*names):
        for n in names:
            if n in row and row[n] not in (None, ""):
                return row[n]
        raise KeyError(f"None of {names} present.")

    return dict(
        last_price=float(pick("last", "lastPrice", "ltp")),
        open=float(pick("open", "openPrice")),
        day_high=float(pick("dayHigh", "high", "high52")) if any(k in row for k in ("dayHigh", "high")) else float(pick("last", "lastPrice")),
        day_low=float(pick("dayLow", "low", "low52")) if any(k in row for k in ("dayLow", "low")) else float(pick("last", "lastPrice")),
        prev_close=float(pick("previousClose", "prevClose", "previous_close")),
    )


# =====================================================================
# State
# =====================================================================
def default_state(date_str):
    return dict(
        date=date_str,
        bot_started_sent=False, premarket_sent=False, market_open_sent=False,
        interval_alerts_sent=[], market_close_sent=False,
        day_open=None,
        last_move_alert_up=0, last_move_alert_down=0,
        last_flip_move_alert_up=0, last_flip_move_alert_down=0,
        ema5=None, ema10=None, prev_gap_sign=None,
        engine_state="WATCHING",    # WATCHING / TRACKING / IN_TRADE
        track_dir=None, track_entry_price=None, track_start_time=None,
        running_fav=0.0, running_abs_gap=0.0,
        in_trade_dir=None,
    )


PERSISTENT_KEYS = ["ema5", "ema10", "prev_gap_sign", "engine_state", "track_dir",
                    "track_entry_price", "track_start_time", "running_fav",
                    "running_abs_gap", "in_trade_dir", "last_flip_move_alert_up", "last_flip_move_alert_down"]


def load_state(today_str):
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            saved = json.load(f)
        if saved.get("date") == today_str:
            return saved
        fresh = default_state(today_str)
        for k in PERSISTENT_KEYS:
            if k in saved:
                fresh[k] = saved[k]
        return fresh
    return default_state(today_str)


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def update_ema(state, price):
    if state["ema5"] is None:
        state["ema5"] = price
        state["ema10"] = price
    else:
        state["ema5"] = price * K5 + state["ema5"] * (1 - K5)
        state["ema10"] = price * K10 + state["ema10"] * (1 - K10)
    return state["ema5"] - state["ema10"]


def fmt_ema_gap(state):
    if state["ema5"] is None or state["ema10"] is None:
        return ""
    gap = state["ema5"] - state["ema10"]
    regime = "in bull" if gap > 0 else ("in bear" if gap < 0 else "flat")
    return f" | EMA Gap: {gap:+.2f} ({regime})"


def get_hourly_status_text(state, price):
    est = state["engine_state"]
    if est == "WATCHING":
        return "Status: No Trade (Watching for EMA flip)"
    elif est == "TRACKING":
        entry = state["track_entry_price"]
        d = state["track_dir"]
        if entry is not None:
            fav = (price - entry) if d == "Bullish" else (entry - price)
            return f"Status: In Noise / Tracking {d} | Pts from flip: {fav:+.1f} pts"
        return f"Status: Tracking {d}"
    elif est == "IN_TRADE":
        entry = state["track_entry_price"]
        d = state["in_trade_dir"]
        if entry is not None:
            fav = (price - entry) if d == "Bullish" else (entry - price)
            return f"Status: In Trade ({d}) | Pts from entry: {fav:+.1f} pts"
        return f"Status: In Trade ({d})"
    return "Status: Unknown"


# =====================================================================
# One run
# =====================================================================
def run(now=None):
    now = now or datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")
    hm = now.strftime("%H:%M")
    state = load_state(today_str)

    if now.weekday() >= 5:   # Sat/Sun
        save_state(state)
        return state

    if not state["bot_started_sent"] and hm >= "09:00":
        send_telegram(f"🤖 NIFTY alert bot started -- {today_str}")
        state["bot_started_sent"] = True

    if not state["premarket_sent"] and hm >= "09:11":
        try:
            q = fetch_nifty_quote()
            chg = q["last_price"] - q["prev_close"]
            status_desc = get_hourly_status_text(state, q["last_price"])
            send_telegram(f"🌅 Pre-market: NIFTY {q['last_price']:.2f} "
                          f"(prev close {q['prev_close']:.2f}, {chg:+.2f} pts){fmt_ema_gap(state)}\n👉 {status_desc}")
        except Exception as e:
            send_telegram(f"⚠️ Pre-market fetch failed: {e}")
        state["premarket_sent"] = True

    if "09:15" <= hm <= "15:30":
        try:
            q = fetch_nifty_quote()
        except Exception as e:
            send_telegram(f"⚠️ NIFTY fetch failed at hm {hm}: {e}")
            save_state(state)
            return state

        price = q["last_price"]
        if state["day_open"] is None:
            state["day_open"] = q["open"]

        if not state["market_open_sent"] and hm >= "09:15":
            status_desc = get_hourly_status_text(state, price)
            send_telegram(f"🔔 Market open. NIFTY {price:.2f} (open {q['open']:.2f}){fmt_ema_gap(state)}\n👉 {status_desc}")
            state["market_open_sent"] = True

        # ---- Every 30 Minutes Status Update Checkpoints ----
        for label in INTERVAL_TIMES:
            if hm >= label and label not in state["interval_alerts_sent"]:
                status_desc = get_hourly_status_text(state, price)
                send_telegram(f"⏱️ {label} Status Update: NIFTY {price:.2f}{fmt_ema_gap(state)}\n👉 {status_desc}")
                state["interval_alerts_sent"].append(label)

        # ---- Point-move from EMA Flip Milestones (Every 50 pts) ----
        if state["engine_state"] in ("TRACKING", "IN_TRADE") and state["track_entry_price"] is not None:
            d = state["track_dir"] or state["in_trade_dir"]
            favorable_move = (price - state["track_entry_price"]) if d == "Bullish" else (state["track_entry_price"] - price)
            
            if favorable_move > 0:
                up_level = int(favorable_move // MOVE_STEP) * int(MOVE_STEP)
                if up_level > state["last_flip_move_alert_up"]:
                    tag = "💯" if up_level % 100 == 0 else "📈"
                    status_desc = get_hourly_status_text(state, price)
                    send_telegram(f"{tag} NIFTY +{up_level} pts favorable from {d} flip ({price:.2f}){fmt_ema_gap(state)}\n👉 {status_desc}")
                    state["last_flip_move_alert_up"] = up_level
            elif favorable_move < 0:
                down_level = int((-favorable_move) // MOVE_STEP) * int(MOVE_STEP)
                if down_level > state["last_flip_move_alert_down"]:
                    tag = "💯" if down_level % 100 == 0 else "📉"
                    status_desc = get_hourly_status_text(state, price)
                    send_telegram(f"{tag} NIFTY -{down_level} pts adverse from {d} flip ({price:.2f}){fmt_ema_gap(state)}\n👉 {status_desc}")
                    state["last_flip_move_alert_down"] = down_level

        # ---- Causal EMA5/10 flip + confirmation state machine ----
        gap = update_ema(state, price)
        gap_sign = 1 if gap > 0 else (-1 if gap < 0 else 0)
        prev_sign = state["prev_gap_sign"]
        is_flip = prev_sign not in (None, 0) and gap_sign != 0 and gap_sign != prev_sign

        if is_flip:
            direction = "Bullish" if gap_sign > 0 else "Bearish"

            if state["engine_state"] == "IN_TRADE" and state["in_trade_dir"] != direction:
                status_desc = get_hourly_status_text(state, price)
                send_telegram(f"🔄 TREND REVERSAL -- closing {state['in_trade_dir']} trade @ {price:.2f}{fmt_ema_gap(state)}\n👉 {status_desc}")
                state["engine_state"] = "WATCHING"
                state["in_trade_dir"] = None

            if state["engine_state"] == "TRACKING":
                status_desc = get_hourly_status_text(state, price)
                send_telegram(f"⚪ NOISE -- {state['track_dir']} flip never confirmed (only {state['running_fav']:.1f} pts reached) -> No trade taken.{fmt_ema_gap(state)}\n👉 {status_desc}")

            if state["engine_state"] in ("WATCHING", "TRACKING"):
                status_desc = get_hourly_status_text(state, price)
                send_telegram(f"🔀 FLIP -- {direction} @ {price:.2f}{fmt_ema_gap(state)}\n👉 {status_desc}")
                state["engine_state"] = "TRACKING"
                state["track_dir"] = direction
                state["track_entry_price"] = price
                state["track_start_time"] = now.isoformat()
                state["running_fav"] = 0.0
                state["running_abs_gap"] = abs(gap)
                state["last_flip_move_alert_up"] = 0
                state["last_flip_move_alert_down"] = 0

        if state["engine_state"] == "TRACKING":
            fav = ((price - state["track_entry_price"]) if state["track_dir"] == "Bullish"
                   else (state["track_entry_price"] - price))
            state["running_fav"] = max(state["running_fav"], fav)
            state["running_abs_gap"] = max(state["running_abs_gap"], abs(gap))

            if state["running_fav"] >= PTS_THRESHOLD:
                status_desc = get_hourly_status_text(state, price)
                send_telegram(f"✅ TRADE CONFIRMED -- {state['track_dir']} @ {price:.2f} (moved {state['running_fav']:.1f} pts from flip){fmt_ema_gap(state)}\n👉 {status_desc}")
                state["engine_state"] = "IN_TRADE"
                state["in_trade_dir"] = state["track_dir"]

        state["prev_gap_sign"] = gap_sign

    if not state["market_close_sent"] and hm >= "15:30":
        rng = ""
        if state["day_open"] is not None:
            rng = f" | open {state['day_open']:.2f}"
        status_desc = get_hourly_status_text(state, price)
        send_telegram(f"🌙 Market closed -- {today_str}{rng}{fmt_ema_gap(state)}\n👉 {status_desc}")
        state["market_close_sent"] = True

    save_state(state)
    return state


if __name__ == "__main__":
    run()
