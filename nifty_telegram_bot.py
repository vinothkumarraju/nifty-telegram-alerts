#!/usr/bin/env python3
"""
NIFTY EMA5/10 Flip -- Telegram Alert Bot (designed to run on GitHub Actions)
==============================================================================

WHAT THIS IS
------------
A 5-minute-resolution sibling of live_replay_engine.py: same causal state
machine (WATCHING -> TRACKING -> IN_TRADE), same idea (never look ahead,
grade each flip only on what's happened so far) -- but running on GitHub
Actions' cron, which can't go faster than ~5 minutes, checking a free/
unofficial NSE endpoint instead of your 1-min historical files. Treat this
as a headline alert stream, not the precise signal engine you'd trade off --
that's still live_replay_engine.py against your real 1-min + options data.

STATE PERSISTENCE
------------------
GitHub Actions runs are stateless -- nothing survives between runs unless
you save it somewhere. This script reads/writes state.json in the repo
working directory; the workflow (nifty_alerts.yml) commits that file back
to the repo after every run. That means your repo will accumulate a commit
roughly every 5 minutes during market hours (~78/day) -- that's normal and
expected for this design. If that commit noise bothers you later, swap to
actions/cache (restore-keys prefix trick) instead -- ask and I'll rewrite it.

DAILY-RESET SCHEDULE (all times IST, best-effort -- see the caveats above)
  09:00            "bot started"
  09:11            pre-market info (previous close, gap)
  09:15            market open marker
  10:15,...,15:15  hourly candle-close snapshot (price + EMA5/EMA10)
  15:30            market-close / day summary
  anytime          FLIP, NOISE, TRADE CONFIRMED, TREND REVERSAL,
                   NIFTY +/-50, NIFTY +/-100 -- fired the moment this
                   run's fetch detects them (so "anytime" really means
                   "at the next run at or after it happens")

ENVIRONMENT VARIABLES (set as GitHub Actions secrets)
  TELEGRAM_BOT_TOKEN   your bot's token from BotFather
  TELEGRAM_CHAT_ID     the chat/channel id to post into
  PTS_THRESHOLD        optional, default 100 (matches your pts_val)
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
MOVE_STEP = 50.0   # every additional 50pt milestone from the day's open gets an alert

CANDLE_TIMES = ["10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]

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
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)
    print("SENT:", text)


# =====================================================================
# Free/unofficial NSE quote fetch -- fragile by nature, always wrapped
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
    # NSE requires a warm-up hit to the homepage to receive cookies --
    # calling the API cold almost always 401s/403s.
    session.get("https://www.nseindia.com", timeout=10)
    time.sleep(1)
    # allIndices lists every index's own OHLC (NOT equity-stockIndices, which
    # returns an index's *constituent stocks* -- that was the bug that
    # produced the 404: right neighborhood, wrong endpoint).
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
        raise RuntimeError(f"'NIFTY 50' not found in allIndices response. "
                            f"First few index labels seen: {seen}. Raw keys of row 0: "
                            f"{list(rows[0].keys()) if rows else 'NO ROWS'}")

    def pick(*names):
        for n in names:
            if n in row and row[n] not in (None, ""):
                return row[n]
        raise KeyError(f"None of {names} present. Available keys: {list(row.keys())}")

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
        candle_alerts_sent=[], market_close_sent=False,
        day_open=None,
        last_move_alert_up=0, last_move_alert_down=0,
        # everything below persists ACROSS days (continuous causal EMA state,
        # exactly like live_replay_engine.py never resets EMA at day boundaries)
        ema5=None, ema10=None, prev_gap_sign=None,
        engine_state="WATCHING",     # WATCHING / TRACKING / IN_TRADE
        track_dir=None, track_entry_price=None, track_start_time=None,
        running_fav=0.0, running_abs_gap=0.0,
        in_trade_dir=None,
    )


PERSISTENT_KEYS = ["ema5", "ema10", "prev_gap_sign", "engine_state", "track_dir",
                   "track_entry_price", "track_start_time", "running_fav",
                   "running_abs_gap", "in_trade_dir"]


def load_state(today_str):
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            saved = json.load(f)
        if saved.get("date") == today_str:
            return saved
        # new day: reset the once-a-day alert flags, but CARRY FORWARD the
        # EMA/flip-tracking state -- the original engine never resets EMA at
        # day boundaries, and neither should this
        fresh = default_state(today_str)
        for k in PERSISTENT_KEYS:
            if k in saved:
                fresh[k] = saved[k]
        return fresh
    return default_state(today_str)   # first run ever


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


def fmt_ema(state):
    if state["ema5"] is None:
        return ""
    return f" | EMA5 {state['ema5']:.2f} / EMA10 {state['ema10']:.2f}"


# =====================================================================
# One run
# =====================================================================
def run(now=None):
    now = now or datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")
    hm = now.strftime("%H:%M")
    state = load_state(today_str)

    if now.weekday() >= 5:   # Sat/Sun -- nothing to do
        save_state(state)
        return state

    # ---- once-daily scheduled alerts ----
    if not state["bot_started_sent"] and hm >= "09:00":
        send_telegram(f"\U0001F916 NIFTY alert bot started -- {today_str}")
        state["bot_started_sent"] = True

    if not state["premarket_sent"] and hm >= "09:11":
        try:
            q = fetch_nifty_quote()
            chg = q["last_price"] - q["prev_close"]
            send_telegram(f"\U0001F305 Pre-market: NIFTY {q['last_price']:.2f} "
                          f"(prev close {q['prev_close']:.2f}, {chg:+.2f} pts)")
        except Exception as e:
            send_telegram(f"\u26A0\uFE0F Pre-market fetch failed: {e}")
        state["premarket_sent"] = True

    # ---- market-hours engine ----
    if "09:15" <= hm <= "15:30":
        try:
            q = fetch_nifty_quote()
        except Exception as e:
            send_telegram(f"\u26A0\uFE0F NIFTY fetch failed at {hm}: {e}")
            save_state(state)
            return state

        price = q["last_price"]
        if state["day_open"] is None:
            state["day_open"] = q["open"]

        if not state["market_open_sent"] and hm >= "09:15":
            send_telegram(f"\U0001F514 Market open. NIFTY {price:.2f} (open {q['open']:.2f})")
            state["market_open_sent"] = True

        for label in CANDLE_TIMES:
            if hm >= label and label not in state["candle_alerts_sent"]:
                send_telegram(f"\U0001F550 {label} candle: NIFTY {price:.2f}{fmt_ema(state)}")
                state["candle_alerts_sent"].append(label)

        # ---- point-move-from-open alerts ----
        move = price - state["day_open"]
        if move > 0:
            up_level = int(move // MOVE_STEP) * int(MOVE_STEP)
            if up_level > state["last_move_alert_up"]:
                tag = "\U0001F4AF" if up_level % 100 == 0 else "\U0001F4C8"
                send_telegram(f"{tag} NIFTY +{up_level} pts from open ({price:.2f})")
                state["last_move_alert_up"] = up_level
        elif move < 0:
            down_level = int((-move) // MOVE_STEP) * int(MOVE_STEP)
            if down_level > state["last_move_alert_down"]:
                tag = "\U0001F4AF" if down_level % 100 == 0 else "\U0001F4C9"
                send_telegram(f"{tag} NIFTY -{down_level} pts from open ({price:.2f})")
                state["last_move_alert_down"] = down_level

        # ---- causal EMA5/10 flip + confirmation state machine (5-min bars) ----
        gap = update_ema(state, price)
        gap_sign = 1 if gap > 0 else (-1 if gap < 0 else 0)
        prev_sign = state["prev_gap_sign"]
        is_flip = prev_sign not in (None, 0) and gap_sign != 0 and gap_sign != prev_sign

        if is_flip:
            direction = "Bullish" if gap_sign > 0 else "Bearish"

            if state["engine_state"] == "IN_TRADE" and state["in_trade_dir"] != direction:
                send_telegram(f"\U0001F501 TREND REVERSAL -- closing {state['in_trade_dir']} @ {price:.2f}")
                state["engine_state"] = "WATCHING"
                state["in_trade_dir"] = None

            if state["engine_state"] == "TRACKING":
                send_telegram(f"\u26AA NOISE -- {state['track_dir']} flip never confirmed "
                              f"(only {state['running_fav']:.1f} pts reached)")

            if state["engine_state"] in ("WATCHING", "TRACKING"):
                send_telegram(f"\U0001F500 FLIP -- {direction} @ {price:.2f}{fmt_ema(state)}")
                state["engine_state"] = "TRACKING"
                state["track_dir"] = direction
                state["track_entry_price"] = price
                state["track_start_time"] = now.isoformat()
                state["running_fav"] = 0.0
                state["running_abs_gap"] = abs(gap)

        if state["engine_state"] == "TRACKING":
            fav = ((price - state["track_entry_price"]) if state["track_dir"] == "Bullish"
                   else (state["track_entry_price"] - price))
            state["running_fav"] = max(state["running_fav"], fav)
            state["running_abs_gap"] = max(state["running_abs_gap"], abs(gap))

            if state["running_fav"] >= PTS_THRESHOLD:
                send_telegram(f"\u2705 TRADE CONFIRMED -- {state['track_dir']} @ {price:.2f} "
                              f"(moved {state['running_fav']:.1f} pts)")
                state["engine_state"] = "IN_TRADE"
                state["in_trade_dir"] = state["track_dir"]

        state["prev_gap_sign"] = gap_sign

    if not state["market_close_sent"] and hm >= "15:30":
        rng = ""
        if state["day_open"] is not None:
            rng = f" | open {state['day_open']:.2f}"
        send_telegram(f"\U0001F319 Market closed -- {today_str}{rng}{fmt_ema(state)}")
        state["market_close_sent"] = True

    save_state(state)
    return state


if __name__ == "__main__":
    run()
