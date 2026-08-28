from datetime import datetime, timedelta, timezone
import json
import os
import time as sleep_time
import traceback
import numpy as np
import pandas as pd
import requests

# ==================== STRATEGY CONFIGURATION ====================
BOT_TOKEN = str(os.getenv("BOT_TOKEN") or "").strip()
CHAT_ID = str(os.getenv("CHAT_ID") or "").strip()

SCAN_INTERVAL_SECONDS = 60  # Fast 60-second live market checks
SPREAD_WIDTH = 200  # 200-point ATM/OTM Debit Spread
GAP_THRESHOLD = 5.0  # EMA Gap compression alert threshold (5.0 pts)
POSITION_QTY = 650  # Position quantity (10 lots @ 65 qty for ₹10L Capital)
STATE_FILE = "strategy_state.json"
TRADE_LOG_FILE = "paper_trades.csv"

# Indian Standard Time (UTC + 5:30)
IST = timezone(timedelta(hours=5, minutes=30))
# ================================================================


def get_target_expiry(dt_ist: datetime) -> str:
  """Dynamically selects the active exchange expiry contract (Tuesday schedule)."""
  today = dt_ist.date()
  days_to_tuesday = (1 - today.weekday()) % 7
  nearest_tuesday = today + timedelta(days=days_to_tuesday)
  dte = (nearest_tuesday - today).days
  target = nearest_tuesday + timedelta(days=7) if dte <= 2 else nearest_tuesday
  return target.strftime("%d-%b-%Y")


def load_state():
  """Loads persistent state from JSON file across runner restarts."""
  default_state = {
      "armed_direction": None,
      "swing_pivot": None,
      "pivot_candle_time": None,
      "armed_candle_time": None,
      "active_position": None,
      "pending_confirmation": None,
      "previous_position_backup": None,
      "last_cross_state": None,
      "last_processed_5m_candle": None,
      "last_verified_1h_candle": None,
      "last_tight_gap_ts": 0,
      "dispatched_slots": [],
  }
  if os.path.exists(STATE_FILE):
    try:
      with open(STATE_FILE, "r") as f:
        data = json.load(f)
        return {**default_state, **data}
    except Exception:
      return default_state
  return default_state


def save_state(state):
  """Saves persistent state to JSON file."""
  with open(STATE_FILE, "w") as f:
    json.dump(state, f, indent=2)


def send_telegram(text: str):
  """Sends formatted Markdown alert cards to Telegram."""
  if not BOT_TOKEN or "YOUR_BOT_TOKEN" in BOT_TOKEN:
    print("Warning: BOT_TOKEN is missing or default.")
    return
  url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
  payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
  try:
    res = requests.post(url, json=payload, timeout=10)
    if res.status_code != 200:
      print(f"Telegram API warning ({res.status_code}): {res.text}")
    else:
      print("Telegram message dispatched successfully.")
  except Exception as e:
    print(f"Telegram dispatch error: {e}")


def send_telegram_error(err_title: str, err_detail: str):
  """Sends an instant red alert card to Telegram whenever an exception occurs."""
  now_ist = datetime.now(IST)
  msg = (
      f"🚨 *NIFTY BOT RUNTIME ERROR / EXCEPTION*\n"
      f"━━━━━━━━━━━━━━━━━━━━━\n"
      f"⏰ *Time:* `{now_ist.strftime('%d-%b %I:%M:%S %p IST')}`\n"
      f"⚠️ *Issue:* `{err_title}`\n"
      f"📋 *Traceback:* `{str(err_detail)[:250]}`\n"
      f"━━━━━━━━━━━━━━━━━━━━━\n"
      f"🔄 *Status:* Attempting auto-recovery in 60 seconds."
  )
  send_telegram(msg)


def log_paper_trade(trade_data):
  """Appends paper trade execution events into paper_trades.csv."""
  df = pd.DataFrame([trade_data])
  if not os.path.exists(TRADE_LOG_FILE):
    df.to_csv(TRADE_LOG_FILE, index=False)
  else:
    df.to_csv(TRADE_LOG_FILE, mode="a", header=False, index=False)


def fetch_yahoo_chart_direct(symbol="%5ENSEI", interval="1h", range_str="5d"):
  """Direct REST API fetch from Yahoo Finance Chart endpoints (Crumb-Free).

  Bypasses yfinance crumb rate limits (HTTP 429) on cloud servers.
  """
  endpoints = [
      f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_str}&interval={interval}",
      f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_str}&interval={interval}",
  ]
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/122.0.0.0 Safari/537.36"
      ),
      "Accept": "application/json",
      "Accept-Language": "en-US,en;q=0.9",
  }

  for url in endpoints:
    try:
      res = requests.get(url, headers=headers, timeout=8)
      if res.status_code == 200:
        data = res.json()
        if (
            "chart" in data
            and "result" in data["chart"]
            and data["chart"]["result"]
        ):
          result = data["chart"]["result"][0]
          timestamps = result.get("timestamp", [])
          quote = result.get("indicators", {}).get("quote", [{}])[0]

          if timestamps and quote:
            df = pd.DataFrame({
                "timestamp": [
                    datetime.fromtimestamp(ts, tz=IST) for ts in timestamps
                ],
                "open": quote.get("open", []),
                "high": quote.get("high", []),
                "low": quote.get("low", []),
                "close": quote.get("close", []),
                "volume": quote.get("volume", []),
            })
            df = df.dropna(subset=["close"]).reset_index(drop=True)
            if not df.empty:
              df.set_index("timestamp", inplace=True)
              return df
    except Exception:
      continue
  return None


def fetch_market_data():
  """Fetches 1H, 5M, and daily close data directly without crumb rate limits.

  Returns: df_1h, df_5m, prev_close
  """
  df_1h = fetch_yahoo_chart_direct("%5ENSEI", interval="1h", range_str="5d")
  df_5m = fetch_yahoo_chart_direct("%5ENSEI", interval="5m", range_str="5d")
  df_daily = fetch_yahoo_chart_direct("%5ENSEI", interval="1d", range_str="5d")

  if df_1h is None or df_1h.empty:
    return None, None, None

  # Calculate 1H EMAs
  df_1h["ema_5"] = df_1h["close"].ewm(span=5, adjust=False).mean()
  df_1h["ema_10"] = df_1h["close"].ewm(span=10, adjust=False).mean()

  prev_close = None
  if df_daily is not None and not df_daily.empty:
    prev_close = (
        float(df_daily["close"].iloc[-2])
        if len(df_daily) >= 2
        else float(df_daily["close"].iloc[-1])
    )

  return df_1h, df_5m, prev_close


def get_prev_close_diff_str(spot: float, prev_close: float) -> str:
  """Formats how many points Nifty is above (+) or below (-) yesterday's close."""
  if prev_close is None or prev_close <= 0:
    return ""
  diff = spot - prev_close
  sign = "+" if diff > 0 else ""
  return f"({sign}{diff:.2f} pts from Prev Close: {prev_close:.2f})"


def get_pre_market_status():
  """Calculates pre-market expected opening gap using direct daily chart endpoints."""
  try:
    df_daily = fetch_yahoo_chart_direct(
        "%5ENSEI", interval="1d", range_str="5d"
    )
    df_1m = fetch_yahoo_chart_direct("%5ENSEI", interval="1m", range_str="1d")

    if df_daily is None or df_daily.empty:
      return None

    prev_close = (
        float(df_daily["close"].iloc[-2])
        if len(df_daily) >= 2
        else float(df_daily["close"].iloc[-1])
    )
    live_spot = (
        float(df_1m["close"].iloc[-1])
        if (df_1m is not None and not df_1m.empty)
        else float(df_daily["close"].iloc[-1])
    )

    gap_pts = live_spot - prev_close
    gap_pct = (gap_pts / prev_close) * 100.0

    return {
        "prev_close": prev_close,
        "pre_market_spot": live_spot,
        "gap_pts": gap_pts,
        "gap_pct": gap_pct,
    }
  except Exception as e:
    print(f"Pre-market fetch notice: {e}")
    return None


def find_first_swing_pivot(df_5m, direction="BULLISH", max_lookback=30):
  """Scans past 5-minute candles backward one by one to find the exact FIRST local swing peak/trough."""
  highs = df_5m["high"].values
  lows = df_5m["low"].values
  n = len(df_5m)

  end_idx = n - 2  # Start from last completed 5m candle
  start_idx = max(1, end_idx - max_lookback)

  if direction == "BULLISH":
    for k in range(end_idx, start_idx, -1):
      is_peak = True
      if k > 0 and highs[k] < highs[k - 1]:
        is_peak = False
      if k < n - 1 and highs[k] < highs[k + 1]:
        is_peak = False
      if is_peak:
        c_time = (
            df_5m.index[k].strftime("%I:%M %p")
            if hasattr(df_5m.index[k], "strftime")
            else str(df_5m.index[k])
        )
        return float(highs[k]), c_time

    fb_k = int(np.argmax(highs[start_idx : end_idx + 1]) + start_idx)
    c_time = (
        df_5m.index[fb_k].strftime("%I:%M %p")
        if hasattr(df_5m.index[fb_k], "strftime")
        else str(df_5m.index[fb_k])
    )
    return float(highs[fb_k]), c_time

  else:
    for k in range(end_idx, start_idx, -1):
      is_trough = True
      if k > 0 and lows[k] > lows[k - 1]:
        is_trough = False
      if k < n - 1 and lows[k] > lows[k + 1]:
        is_trough = False
      if is_trough:
        c_time = (
            df_5m.index[k].strftime("%I:%M %p")
            if hasattr(df_5m.index[k], "strftime")
            else str(df_5m.index[k])
        )
        return float(lows[k]), c_time

    fb_k = int(np.argmin(lows[start_idx : end_idx + 1]) + start_idx)
    c_time = (
        df_5m.index[fb_k].strftime("%I:%M %p")
        if hasattr(df_5m.index[fb_k], "strftime")
        else str(df_5m.index[fb_k])
    )
    return float(lows[fb_k]), c_time


def execute_spread(
    trade_type,
    spot,
    target_expiry,
    time_str,
    state,
    reason,
    prev_close=None,
):
  """Squares off existing opposing spread (Leg 1) and opens a new 200-pt debit spread (Leg 2)

  with full multi-leg paper trade logging and backup preservation for rollback
  defense.
  """
  atm_strike = int(round(spot / 50.0) * 50)
  exit_msg_block = ""

  if state["active_position"] is not None:
    old_pos = state["active_position"]
    points_captured = (
        (spot - old_pos["entry_spot"])
        if "BULL" in old_pos["type"]
        else (old_pos["entry_spot"] - spot)
    )
    rupee_pnl = (
        points_captured * 0.22 * POSITION_QTY
    )  # 200-pt spread delta ~0.22
    pnl_str = (
        f"+{points_captured:.2f} pts (~+₹{rupee_pnl:,.0f})"
        if points_captured > 0
        else f"{points_captured:.2f} pts (~-₹{abs(rupee_pnl):,.0f})"
    )

    state["previous_position_backup"] = {
        **old_pos,
        "exit_spot": spot,
        "exit_time": time_str,
        "captured_pnl_pts": round(points_captured, 2),
        "captured_inr": round(rupee_pnl, 2),
    }

    exit_msg_block = (
        f"🔄 *SQUARED OFF PREVIOUS POSITION (LEG 1)*\n"
        f"Type: `{old_pos['type']}` | Expiry: `{old_pos['expiry']}`\n"
        f"Strikes: `{old_pos['buy_strike']} / {old_pos['sell_strike']}`\n"
        f"Captured Spot PnL: `{pnl_str}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
    )
    log_paper_trade({
        "timestamp": time_str,
        "action": "CLOSE_FOR_NEW_ENTRY_LEG1",
        "direction": old_pos["type"],
        "expiry": old_pos["expiry"],
        "spot": spot,
        "buy_strike": old_pos["buy_strike"],
        "sell_strike": old_pos["sell_strike"],
        "pnl_pts": round(points_captured, 2),
        "est_net_inr": round(rupee_pnl, 2),
    })
  else:
    state["previous_position_backup"] = None

  if "BULL" in trade_type:
    buy_strike = atm_strike
    sell_strike = atm_strike + SPREAD_WIDTH
    spread_name = (
        f"BULL CALL DEBIT SPREAD ({buy_strike} CE / {sell_strike} CE)"
    )
  else:
    buy_strike = atm_strike
    sell_strike = atm_strike - SPREAD_WIDTH
    spread_name = f"BEAR PUT DEBIT SPREAD ({buy_strike} PE / {sell_strike} PE)"

  state["active_position"] = {
      "type": trade_type,
      "expiry": target_expiry,
      "buy_strike": buy_strike,
      "sell_strike": sell_strike,
      "entry_spot": spot,
      "entry_time": time_str,
      "is_restored_leg3": False,
  }

  action_tag = (
      f"OPEN_{reason}_LEG2"
      if "BREAKOUT" in reason or "GAP" in reason
      else f"OPEN_{reason}"
  )
  log_paper_trade({
      "timestamp": time_str,
      "action": action_tag,
      "direction": trade_type,
      "expiry": target_expiry,
      "spot": spot,
      "buy_strike": buy_strike,
      "sell_strike": sell_strike,
      "pnl_pts": "",
      "est_net_inr": "",
  })

  return exit_msg_block, spread_name, buy_strike, sell_strike


def evaluate_and_notify():
  """Main strategy evaluator executed every 60 seconds with full 3-Leg rollback execution."""
  state = load_state()
  now_ist = datetime.now(IST)
  now_ts = sleep_time.time()
  current_time_str = now_ist.strftime("%I:%M:%S %p IST")
  date_str = now_ist.strftime("%Y-%m-%d")
  hour = now_ist.hour
  minute = now_ist.minute
  target_expiry = get_target_expiry(now_ist)

  # 1. Pre-Market Gap Card (09:08 to 09:14 AM IST)
  if hour == 9 and (8 <= minute <= 14):
    slot_id = f"{date_str}_PRE_MARKET"
    if slot_id not in state["dispatched_slots"]:
      pre_data = get_pre_market_status()
      if pre_data:
        state["dispatched_slots"].append(slot_id)
        save_state(state)

        gap_pts = pre_data["gap_pts"]
        gap_pct = pre_data["gap_pct"]
        sentiment = (
            "🟢 *GAP UP OPENING EXPECTED*"
            if gap_pts > 15
            else (
                "🔴 *GAP DOWN OPENING EXPECTED*"
                if gap_pts < -15
                else "⚪ *FLAT OPENING EXPECTED*"
            )
        )

        msg = (
            f"🔔 *NIFTY PRE-MARKET SESSION UPDATE*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ *Time:* `{now_ist.strftime('%d-%b %I:%M %p IST')}`\n"
            f"{sentiment}\n\n"
            f"📍 *Indicative Spot:* `{pre_data['pre_market_spot']:.2f}`\n"
            f"⏮️ *Previous Close:* `{pre_data['prev_close']:.2f}`\n"
            f"📏 *Expected Gap:* `{gap_pts:+.2f} pts` (`{gap_pct:+.2f}%`)\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👉 *Plan:* Awaiting regular market open (09:15 AM) for instant"
            " gap-open crossover or 5m breakout verification."
        )
        send_telegram(msg)
        print(f"[{current_time_str}] Dispatched Pre-Market Update.")

  df_1h, df_5m, prev_close = fetch_market_data()
  if df_1h is None or df_1h.empty:
    return

  spot = (
      float(df_5m["close"].iloc[-1])
      if (df_5m is not None and not df_5m.empty)
      else float(df_1h["close"].iloc[-1])
  )
  diff_str = get_prev_close_diff_str(spot, prev_close)

  curr_1h = df_1h.iloc[-1]
  prev_1h = df_1h.iloc[-2]

  prev_ema5 = float(prev_1h["ema_5"])
  prev_ema10 = float(prev_1h["ema_10"])

  # Live 1H EMA formula (Fast α=1/3, Slow α=2/11)
  k5, k10 = 2.0 / 6.0, 2.0 / 11.0
  e5 = (spot * k5) + (prev_ema5 * (1.0 - k5))
  e10 = (spot * k10) + (prev_ema10 * (1.0 - k10))
  gap = abs(e5 - e10)

  s_invalidation = (27.0 * prev_ema10 - 22.0 * prev_ema5) / 5.0
  safety_buffer = abs(spot - s_invalidation)

  closed_1h_time = (
      df_1h.index[-2].strftime("%d-%b %I:%M %p")
      if hasattr(df_1h.index[-2], "strftime")
      else str(df_1h.index[-2])
  )
  forming_1h_time = (
      df_1h.index[-1].strftime("%d-%b %I:%M %p")
      if hasattr(df_1h.index[-1], "strftime")
      else str(df_1h.index[-1])
  )

  trend = (
      "Bullish (5 EMA > 10 EMA)" if e5 > e10 else "Bearish (5 EMA < 10 EMA)"
  )

  print(
      f"[{current_time_str}] Spot: {spot:.2f} {diff_str} | 5 EMA: {e5:.2f} | 10"
      f" EMA: {e10:.2f} | Gap: {gap:.2f} pts"
  )

  # ==========================================================
  # 2. 1H CANDLE CLOSE AUDIT & 3-LEG ROLLBACK ENGINE
  # ==========================================================
  if state.get("last_verified_1h_candle") != closed_1h_time:
    closed_ema5 = float(prev_1h["ema_5"])
    closed_ema10 = float(prev_1h["ema_10"])
    closed_gap = abs(closed_ema5 - closed_ema10)

    # 2A. Audit Pending Breakout Trade on 1H Candle Close
    if state["pending_confirmation"] is not None:
      pending = state["pending_confirmation"]
      if pending["candle_time"] == closed_1h_time:
        state["last_verified_1h_candle"] = closed_1h_time
        req_direction = pending["expected_direction"]

        confirmed = (
            (req_direction == "BULLISH" and closed_ema5 > closed_ema10)
            or (req_direction == "BEARISH" and closed_ema5 < closed_ema10)
        )

        if confirmed:
          state["pending_confirmation"] = None
          state["previous_position_backup"] = None
          save_state(state)
          msg = (
              f"🔒 *1H CANDLE CLOSE CONFIRMED — POSITION LOCKED*\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
              f"⏰ *1H Candle Closed:* `{closed_1h_time}`\n"
              f"📈 *Final 5 EMA:* `{closed_ema5:.2f}` | *10 EMA:*"
              f" `{closed_ema10:.2f}`\n"
              f"📏 *Confirmed Gap:* `{closed_gap:.2f} pts`\n"
              f"📦 *Locked Position:* `{state['active_position']['type']}`\n"
              f"📅 *Expiry:* `{state['active_position']['expiry']}`\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
              f"✅ *Status:* 1H crossover verified and locked for subsequent"
              " market sessions."
          )
          send_telegram(msg)
        else:
          # Crossover FAILED -> Execute 3-Leg Physical Rollback Defense
          failed_pos = state["active_position"]
          backup_pos = state["previous_position_backup"]
          pnl_loss = (
              (spot - failed_pos["entry_spot"])
              if "BULL" in failed_pos["type"]
              else (failed_pos["entry_spot"] - spot)
          )
          rupee_loss = pnl_loss * 0.22 * POSITION_QTY

          # Log Leg 2 (Trial Breakout) Close
          log_paper_trade({
              "timestamp": current_time_str,
              "action": "CLOSE_ROLLBACK_INVALIDATED_LEG2",
              "direction": failed_pos["type"],
              "expiry": failed_pos["expiry"],
              "spot": spot,
              "buy_strike": failed_pos["buy_strike"],
              "sell_strike": failed_pos["sell_strike"],
              "pnl_pts": round(pnl_loss, 2),
              "est_net_inr": round(rupee_loss, 2),
          })

          # Re-open Leg 3 (Restored Parent Trend Spread)
          reopened_type = (
              backup_pos["type"]
              if backup_pos
              else (
                  "BEAR_PUT_SPREAD"
                  if req_direction == "BULLISH"
                  else "BULL_CALL_SPREAD"
              )
          )
          atm_strike = int(round(spot / 50.0) * 50)
          b_strike = atm_strike
          s_strike = (
              atm_strike + SPREAD_WIDTH
              if "BULL" in reopened_type
              else atm_strike - SPREAD_WIDTH
          )

          leg1_pnl_pts = (
              backup_pos.get("captured_pnl_pts", 0.0) if backup_pos else 0.0
          )

          state["active_position"] = {
              "type": reopened_type,
              "expiry": target_expiry,
              "buy_strike": b_strike,
              "sell_strike": s_strike,
              "entry_spot": spot,
              "entry_time": current_time_str,
              "parent_leg1_pnl": leg1_pnl_pts,
              "trial_leg2_pnl": round(pnl_loss, 2),
              "is_restored_leg3": True,
          }
          state["pending_confirmation"] = None
          state["previous_position_backup"] = None
          state["armed_direction"] = None
          state["swing_pivot"] = None
          save_state(state)

          # Log Leg 3 Entry
          log_paper_trade({
              "timestamp": current_time_str,
              "action": "OPEN_ROLLBACK_RESTORED_LEG3",
              "direction": reopened_type,
              "expiry": target_expiry,
              "spot": spot,
              "buy_strike": b_strike,
              "sell_strike": s_strike,
              "pnl_pts": "",
              "est_net_inr": "",
          })

          rollback_msg = (
              f"🚨 *1H CROSSOVER FAILED — 3-LEG ROLLBACK DEFENSE TRIGGERED*\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
              f"⏰ *1H Candle Closed:* `{closed_1h_time}` | *Spot:* `{spot:.2f}`"
              f" {diff_str}\n"
              f"⚠️ *Failure Reason:* 5 EMA failed to hold beyond 10 EMA at"
              " candle close (False Wick Trapped).\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
              f"🛑 *LEG 1 (Parent Trend):*"
              f" `{backup_pos['type'] if backup_pos else 'PREV_TREND'}` |"
              f" Captured: `{leg1_pnl_pts:+.2f} pts`\n"
              f"❌ *LEG 2 (Trial Breakout):* Exited `{failed_pos['type']}` |"
              f" Loss: `{pnl_loss:+.2f} pts` (~₹{rupee_loss:,.0f})\n"
              f"🔄 *LEG 3 (Restored Trend):* Re-opened `{reopened_type}`"
              f" ({b_strike} / {s_strike}) @ `{spot:.2f}`\n"
              f"📅 *Target Expiry:* `{target_expiry}`\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
              f"🛡️ *Defense Strategy:* Cut trial breakout after 1H audit and"
              " restored active higher-timeframe trend."
          )
          send_telegram(rollback_msg)

    # 2B. Fallback Entry: 1H Candle Closed Confirmed with no earlier 5m breakout
    elif (
        state["armed_direction"] is not None
        and state["pending_confirmation"] is None
    ):
      req_dir = state["armed_direction"]
      is_1h_confirmed = (
          (req_dir == "BULLISH" and closed_ema5 > closed_ema10)
          or (req_dir == "BEARISH" and closed_ema5 < closed_ema10)
      )

      if is_1h_confirmed:
        trade_type = (
            "BULL_CALL_SPREAD" if req_dir == "BULLISH" else "BEAR_PUT_SPREAD"
        )
        if (
            state.get("active_position") is None
            or state["active_position"]["type"] != trade_type
        ):
          state["last_verified_1h_candle"] = closed_1h_time
          exit_block, spread_name, b_strike, s_strike = execute_spread(
              trade_type,
              spot,
              target_expiry,
              current_time_str,
              state,
              reason="1H_CLOSE_CONFIRM",
              prev_close=prev_close,
          )

          state["armed_direction"] = None
          state["swing_pivot"] = None
          state["pending_confirmation"] = None
          save_state(state)

          fallback_msg = (
              f"🚀 *TRADE EXECUTED ON 1H CANDLE CLOSE CONFIRMATION*\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
              f"{exit_block}"
              f"📦 *Position:* `{spread_name}`\n"
              f"📅 *Contract Expiry:* `{target_expiry}`\n"
              f"⏰ *Executed At:* `{current_time_str}` (1H Close:"
              f" `{closed_1h_time}`)\n"
              f"📍 *Spot:* `{spot:.2f}` {diff_str}\n"
              f"📈 *Closed 5 EMA:* `{closed_ema5:.2f}` | *10 EMA:*"
              f" `{closed_ema10:.2f}`\n"
              f"📏 *Closed 1H Gap:* `{closed_gap:.2f} pts`\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
              f"🔒 *Reason:* 5m breakout did not trigger during the hour, but 1H"
              " crossover finalized and locked on close."
          )
          send_telegram(fallback_msg)
        else:
          state["armed_direction"] = None
          state["swing_pivot"] = None
          save_state(state)
      else:
        state["armed_direction"] = None
        state["swing_pivot"] = None
        save_state(state)

  # ==========================================================
  # 3. 09:15 AM INSTANT GAP OPEN EXECUTION (NO 5M WAIT)
  # ==========================================================
  bull_cross = (e5 > e10) and (prev_ema5 <= prev_ema10)
  bear_cross = (e5 < e10) and (prev_ema5 >= prev_ema10)

  if hour == 9 and minute == 15:
    if bull_cross or bear_cross:
      trade_type = "BULL_CALL_SPREAD" if bull_cross else "BEAR_PUT_SPREAD"
      if (
          state.get("active_position") is None
          or state["active_position"]["type"] != trade_type
      ):
        exit_block, spread_name, b_strike, s_strike = execute_spread(
            trade_type,
            spot,
            target_expiry,
            current_time_str,
            state,
            reason="0915_GAP_OPEN",
            prev_close=prev_close,
        )
        state["pending_confirmation"] = {
            "candle_time": forming_1h_time,
            "expected_direction": "BULLISH" if bull_cross else "BEARISH",
        }
        state["last_cross_state"] = "BULL" if bull_cross else "BEAR"
        save_state(state)

        gap_open_msg = (
            f"⚡ *09:15 AM INSTANT GAP-OPEN ENTRY EXECUTED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{exit_block}"
            f"📦 *Position:* `{spread_name}`\n"
            f"📅 *Expiry:* `{target_expiry}`\n"
            f"⏰ *Time:* `{current_time_str}` (Market Open Bell)\n"
            f"📍 *Open Spot:* `{spot:.2f}` {diff_str}\n"
            f"📈 *Live 5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}`\n"
            f"🔒 *Floor Price:* `{s_invalidation:.2f}` (Buffer:"
            f" `{safety_buffer:.2f} pts`)\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 *Reason:* Overnight gap caused instant 1H crossover on opening"
            " tick (bypassed 5m candle wait to eliminate slippage)."
        )
        send_telegram(gap_open_msg)
        return

  # ==========================================================
  # 4. 03:15 PM - 03:30 PM EOD CLOSING CROSSOVER & BTST/STBT SPREAD EXECUTION
  # ==========================================================
  if hour == 15 and (15 <= minute <= 30):
    # Verify if an actual crossover happened between the previous closed 1H bar and live
    is_fresh_bull_cross = (e5 > e10) and (prev_ema5 <= prev_ema10)
    is_fresh_bear_cross = (e5 < e10) and (prev_ema5 >= prev_ema10)
    
    active_type = (
        state.get("active_position", {}).get("type")
        if state.get("active_position")
        else None
    )

    if (is_fresh_bull_cross and active_type != "BULL_CALL_SPREAD") or (is_fresh_bear_cross and active_type != "BEAR_PUT_SPREAD"):
      eod_slot = f"{date_str}_15_30_EOD_CROSS_EXEC"
      if eod_slot not in state["dispatched_slots"]:
        state["dispatched_slots"].append(eod_slot)
        save_state(state)
        
        required_eod_type = "BULL_CALL_SPREAD" if is_fresh_bull_cross else "BEAR_PUT_SPREAD"

        exit_block, spread_name, b_strike, s_strike = execute_spread(
            required_eod_type,
            spot,
            target_expiry,
            current_time_str,
            state,
            reason="1530_EOD_CLOSING_CROSSOVER",
            prev_close=prev_close,
        )

        state["armed_direction"] = None
        state["swing_pivot"] = None
        state["pending_confirmation"] = None
        state["last_cross_state"] = "BULL" if is_fresh_bull_cross else "BEAR"
        save_state(state)

        eod_cross_msg = (
            f"🎯 *03:15–03:30 PM EOD CROSSOVER EXECUTED & SPREAD ALERT*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{exit_block}"
            f"📦 *Position:* `{spread_name}`\n"
            f"📅 *Target Expiry:* `{target_expiry}`\n"
            f"⏰ *Time:* `{current_time_str}` (EOD Market Closing Session)\n"
            f"📍 *Closing Spot:* `{spot:.2f}` {diff_str}\n"
            f"📈 *Live 5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}`\n"
            f"📏 *EMA Gap:* `{gap:.2f} pts`\n"
            f"🔒 *Floor Price:* `{s_invalidation:.2f}` (Buffer:"
            f" `{safety_buffer:.2f} pts`)\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 *Action:* 1-Hour EMA crossover finalized during 03:15–03:30 PM"
            " closing window. Opened 200-pt debit spread for overnight carry"
            " (BTST/STBT)."
        )
        send_telegram(eod_cross_msg)
        return

  # ==========================================================
  # 5. 1H CROSSOVER DETECTION -> DYNAMIC BACKWARD SCAN (INTRA-DAY)
  # ==========================================================
  if df_5m is not None and not df_5m.empty and hour < 15:
    if bull_cross and state["last_cross_state"] != "BULL":
      first_swing_high, pivot_time = find_first_swing_pivot(
          df_5m, direction="BULLISH"
      )
      state["armed_direction"] = "BULLISH"
      state["swing_pivot"] = first_swing_high
      state["pivot_candle_time"] = pivot_time
      state["armed_candle_time"] = forming_1h_time
      state["last_cross_state"] = "BULL"
      save_state(state)

      cross_msg = (
          f"🎯 *1H BULLISH CROSSOVER DETECTED (ARMED)*\n"
          f"━━━━━━━━━━━━━━━━━━━━━\n"
          f"⏰ *Time:* `{now_ist.strftime('%I:%M %p IST')}`\n"
          f"📍 *Current Spot:* `{spot:.2f}` {diff_str}\n"
          f"📈 *Live 5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}` (Gap:"
          f" `{gap:.2f} pts`)\n"
          f"🔒 *Crossover Invalidation Floor:* `{s_invalidation:.2f}`\n"
          f"🛡️ *Spot Safety Buffer:* `{safety_buffer:.2f} pts`\n"
          f"━━━━━━━━━━━━━━━━━━━━━\n"
          f"🎯 *First Swing High Found:* `{first_swing_high:.2f}` (Bar:"
          f" `{pivot_time}`)\n"
          f"📅 *Target Expiry:* `{target_expiry}`\n"
          f"━━━━━━━━━━━━━━━━━━━━━\n"
          f"👉 *Next Action:* Watching for live price to cross >"
          f" `{first_swing_high:.2f}` to trigger immediate Bull Call Spread."
      )
      send_telegram(cross_msg)

    elif bear_cross and state["last_cross_state"] != "BEAR":
      first_swing_low, pivot_time = find_first_swing_pivot(
          df_5m, direction="BEARISH"
      )
      state["armed_direction"] = "BEARISH"
      state["swing_pivot"] = first_swing_low
      state["pivot_candle_time"] = pivot_time
      state["armed_candle_time"] = forming_1h_time
      state["last_cross_state"] = "BEAR"
      save_state(state)

      cross_msg = (
          f"🎯 *1H BEARISH CROSSOVER DETECTED (ARMED)*\n"
          f"━━━━━━━━━━━━━━━━━━━━━\n"
          f"⏰ *Time:* `{now_ist.strftime('%I:%M %p IST')}`\n"
          f"📍 *Current Spot:* `{spot:.2f}` {diff_str}\n"
          f"📉 *Live 5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}` (Gap:"
          f" `{gap:.2f} pts`)\n"
          f"🔒 *Crossover Invalidation Floor:* `{s_invalidation:.2f}`\n"
          f"🛡️ *Spot Safety Buffer:* `{safety_buffer:.2f} pts`\n"
          f"━━━━━━━━━━━━━━━━━━━━━\n"
          f"🎯 *First Swing Low Found:* `{first_swing_low:.2f}` (Bar:"
          f" `{pivot_time}`)\n"
          f"📅 *Target Expiry:* `{target_expiry}`\n"
          f"━━━━━━━━━━━━━━━━━━━━━\n"
          f"👉 *Next Action:* Watching for live price to cross <"
          f" `{first_swing_low:.2f}` to trigger immediate Bear Put Spread."
      )
      send_telegram(cross_msg)

    # ==========================================================
    # 6. REAL-TIME 5M PIVOT BREAKOUT EXECUTION (NO 5M WAIT)
    # ==========================================================
    if state["armed_direction"] is not None and state["swing_pivot"] is not None:
      direction = state["armed_direction"]
      pivot = float(state["swing_pivot"])

      is_bull_cross = (direction == "BULLISH") and (spot > pivot)
      is_bear_cross = (direction == "BEARISH") and (spot < pivot)

      if is_bull_cross or is_bear_cross:
        trade_type = (
            "BULL_CALL_SPREAD" if is_bull_cross else "BEAR_PUT_SPREAD"
        )

        if (
            state.get("active_position") is None
            or state["active_position"]["type"] != trade_type
        ):
          exit_block, spread_name, b_strike, s_strike = execute_spread(
              trade_type,
              spot,
              target_expiry,
              current_time_str,
              state,
              reason="DYNAMIC_PIVOT_LIVE_BREAKOUT",
              prev_close=prev_close,
          )

          state["pending_confirmation"] = {
              "candle_time": forming_1h_time,
              "expected_direction": direction,
          }
          state["armed_direction"] = None
          state["swing_pivot"] = None
          save_state(state)

          exec_msg = (
              f"🚀 *TRADE EXECUTED ON DYNAMIC PIVOT BREAKOUT*\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
              f"{exit_block}"
              f"📦 *Position:* `{spread_name}`\n"
              f"📅 *Expiry:* `{target_expiry}`\n"
              f"⏰ *Execution Time:* `{current_time_str}` (Live Tick Trigger)\n"
              f"📍 *Entry Spot:* `{spot:.2f}` {diff_str}\n"
              f"🎯 *Breached First Swing Pivot:* `{pivot:.2f}`\n"
              f"🔒 *Floor Price:* `{s_invalidation:.2f}` (Buffer:"
              f" `{safety_buffer:.2f} pts`)\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
              f"⏳ *Audit Status:* Tagged `PENDING_1H_CLOSE`. Awaiting 1H candle"
              f" close (`{forming_1h_time}`) to verify and lock position."
          )
          send_telegram(exec_msg)
        else:
          state["armed_direction"] = None
          state["swing_pivot"] = None
          save_state(state)

  # ==========================================================
  # 7. TIGHT GAP WARNING (<= 5.0 PTS) WITH 15-MIN COOLDOWN
  # ==========================================================
  elif gap <= GAP_THRESHOLD and hour < 15:
    if (now_ts - state.get("last_tight_gap_ts", 0)) > 900:
      state["last_tight_gap_ts"] = now_ts
      save_state(state)
      msg = (
          f"⚠️ *LIVE WARNING: 1H EMA GAP <= 5.0 PTS*\n"
          f"━━━━━━━━━━━━━━━━━━━━━\n"
          f"⏰ *Time:* `{now_ist.strftime('%I:%M %p IST')}`\n"
          f"📍 *Spot:* `{spot:.2f}` {diff_str}\n"
          f"📏 *Live Gap:* `{gap:.2f} pts`\n"
          f"🧭 *Current Alignment:* `{trend}`\n"
          f"━━━━━━━━━━━━━━━━━━━━━\n"
          f"👉 *Status:* EMA compression active. Watching for imminent"
          " crossover."
      )
      send_telegram(msg)

  # ==========================================================
  # 8. SCHEDULED 30-MINUTE STATUS UPDATES & EOD CAS FINAL CARD
  # ==========================================================
  target_slot_type = None
  target_slot_id = None

  if 15 <= minute <= 20:
    target_slot_id = f"{date_str}_{hour:02d}_15"
    target_slot_type = "30MIN_STATUS"
  elif (45 <= minute <= 50) and (hour < 15):
    target_slot_id = f"{date_str}_{hour:02d}_45"
    target_slot_type = "30MIN_STATUS"
  elif hour == 15 and (30 <= minute <= 35):
    target_slot_id = f"{date_str}_EOD_CAS"
    target_slot_type = "EOD_CAS"

  if target_slot_id and target_slot_id not in state["dispatched_slots"]:
    state["dispatched_slots"].append(target_slot_id)
    save_state(state)

    active_pos = state.get("active_position")
    if active_pos:
      active_spread_info = (
          f"📦 *Active Position:* `{active_pos['type']}`\n"
          f"🎯 *Strikes:* `{active_pos['buy_strike']} /"
          f" {active_pos['sell_strike']}`\n"
          f"📅 *Expiry:* `{active_pos['expiry']}`"
      )
    else:
      atm_strk = int(round(spot / 50.0) * 50)
      rec_spread = (
          f"BULL CALL DEBIT SPREAD ({atm_strk} CE / {atm_strk + 200} CE)"
          if (e5 > e10)
          else f"BEAR PUT DEBIT SPREAD ({atm_strk} PE / {atm_strk - 200} PE)"
      )
      active_spread_info = (
          "📦 *Active Position:* `None (Flat)`\n"
          f"👉 *Recommended Spread:* `{rec_spread}`"
      )

    if target_slot_type == "EOD_CAS":
      title = (
          "📊 *NIFTY 1H: EOD POST-CAS FINAL & OVERNIGHT SPREAD REPORT*"
      )
      status_line = (
          "✅ *Status:* Market Closed (3:30 PM) & CAS Settled.\n"
          f"{active_spread_info}"
      )
    elif hour == 9 and minute <= 20:
      title = "📊 *NIFTY 1H: MARKET OPEN STATUS (09:15 AM)*"
      status_line = "🚀 *Status:* Regular Session Active."
    elif 15 <= minute <= 20:
      title = (
          f"📊 *NIFTY 1H: HOURLY CANDLE CLOSE UPDATE"
          f" ({now_ist.strftime('%I:15 %p')})*"
      )
      status_line = "🔒 *Status:* 1-Hour Candle Cycle Finalized."
    else:
      title = (
          f"📊 *NIFTY 1H: MID-HOUR STATUS UPDATE"
          f" ({now_ist.strftime('%I:45 %p')})*"
      )
      status_line = "⏳ *Status:* Mid-Hour Trend Checkpoint."

    msg = (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ *Time:* `{now_ist.strftime('%d-%b %I:%M %p IST')}`\n"
        f"📍 *Nifty Spot:* `{spot:.2f}` {diff_str}\n"
        f"📈 *Live 5 EMA:* `{e5:.2f}`\n"
        f"📉 *Live 10 EMA:* `{e10:.2f}`\n"
        f"📏 *EMA Gap:* `{gap:.2f} pts`\n"
        f"🔒 *Floor Price:* `{s_invalidation:.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧭 *Trend Alignment:* `{trend}`\n"
        f"{status_line}"
    )
    send_telegram(msg)
    print(f"[{current_time_str}] Dispatched Scheduled Update: {target_slot_id}")


def run_live_loop():
  print("🚀 Nifty Live Scanner & Paper Trader Initialized.")
  now_ist = datetime.now(IST)
  current_minutes = now_ist.hour * 60 + now_ist.minute

  # 1. Startup Ping to Telegram
  try:
    df_1h, df_5m, prev_close = fetch_market_data()
    spot_info = ""
    trend_info = "Market Offline / Closed"

    if df_1h is not None and not df_1h.empty:
      spot = (
          float(df_5m["close"].iloc[-1])
          if (df_5m is not None and not df_5m.empty)
          else float(df_1h["close"].iloc[-1])
      )
      diff_str = get_prev_close_diff_str(spot, prev_close)
      spot_info = f"📍 *Latest Spot:* `{spot:.2f}` {diff_str}\n"

      if len(df_1h) >= 2:
        prev_1h = df_1h.iloc[-2]
        prev_ema5 = float(prev_1h["ema_5"])
        prev_ema10 = float(prev_1h["ema_10"])
        k5, k10 = 2.0 / 6.0, 2.0 / 11.0
        e5 = (spot * k5) + (prev_ema5 * (1.0 - k5))
        e10 = (spot * k10) + (prev_ema10 * (1.0 - k10))
        gap = abs(e5 - e10)
        trend = (
            "Bullish (5 EMA > 10 EMA)"
            if e5 > e10
            else "Bearish (5 EMA < 10 EMA)"
        )
        trend_info = (
            f"📈 *Live 5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}` (Gap:"
            f" `{gap:.2f}` pts)\n🧭 *Trend:* `{trend}`"
        )
    else:
      spot_info = "📍 *Spot Data:* `Awaiting Live Market Tick`\n"

    session_status = (
        "🟢 *Live Market Session Active*"
        if (9 * 60 + 5) <= current_minutes <= (15 * 60 + 35)
        else "⚪ *Post-Market / Manual Test Run*"
    )

    startup_msg = (
        f"🟢 *NIFTY BOT CONNECTED & ONLINE*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ *Time:* `{now_ist.strftime('%d-%b %I:%M:%S %p IST')}`\n"
        f"{spot_info}"
        f"{trend_info}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ *Runner Status:* {session_status}\n"
        f"🤖 *Bot Token & Chat ID:* `Verified & Operational`"
    )
    send_telegram(startup_msg)
    print(
        f"[{now_ist.strftime('%I:%M:%S %p IST')}] Dispatched Instant Startup"
        " Ping to Telegram."
    )
  except Exception as e:
    err_trace = traceback.format_exc()
    print(f"Startup ping error: {e}")
    send_telegram_error("Startup Ping Failed", err_trace)

  # 2. Exit cleanly if triggered after market hours on non-trading days
  if current_minutes > (15 * 60 + 35):
    print(
        f"Market is closed ({now_ist.strftime('%I:%M %p IST')}). Startup test"
        " verified. Exiting."
    )
    return

  # 3. Continuous Execution Loop
  while True:
    loop_ist = datetime.now(IST)
    loop_mins = loop_ist.hour * 60 + loop_ist.minute

    # End of normal day market session
    if loop_mins > (15 * 60 + 35):
      print(
          f"🏁 Market Closed ({loop_ist.strftime('%I:%M %p IST')}). Session"
          " complete."
      )
      break

    # Weekday Holiday Detection (No new candles generated by 09:30 AM)
    if loop_mins >= (9 * 60 + 30) and loop_ist.weekday() < 5:
      df_1h_check, _, _ = fetch_market_data()
      if df_1h_check is not None and not df_1h_check.empty:
        latest_date = (
            df_1h_check.index[-1].date()
            if hasattr(df_1h_check.index[-1], "date")
            else None
        )
        if latest_date and latest_date < loop_ist.date():
          holiday_msg = (
              f"🏖️ *NSE EXCHANGE HOLIDAY DETECTED*\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
              f"⏰ *Time:* `{loop_ist.strftime('%d-%b %I:%M %p IST')}`\n"
              f"ℹ️ *Status:* No live ticks registered for today by 09:30 AM.\n"
              f"💤 *Action:* Bot is entering sleep mode to save server minutes."
          )
          send_telegram(holiday_msg)
          print("Exchange holiday detected. Shutting down runner cleanly.")
          break

    try:
      evaluate_and_notify()
    except Exception as e:
      err_trace = traceback.format_exc()
      print(f"Execution cycle error: {e}")
      send_telegram_error("Live Strategy Evaluation Error", err_trace)

    sleep_time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
  run_live_loop()
