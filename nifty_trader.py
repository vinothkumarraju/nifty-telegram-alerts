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
MAX_TRIAL_LOSS_PTS = 45.0  # Absolute maximum points you are willing to risk on a false breakout
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
  endpoints = [
      f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_str}&interval={interval}",
      f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_str}&interval={interval}",
  ]
  headers = {
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
  df_1h = fetch_yahoo_chart_direct("%5ENSEI", interval="1h", range_str="5d")
  df_5m = fetch_yahoo_chart_direct("%5ENSEI", interval="5m", range_str="5d")
  df_daily = fetch_yahoo_chart_direct("%5ENSEI", interval="1d", range_str="5d")

  if df_1h is None or df_1h.empty:
    return None, None, None

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
  if prev_close is None or prev_close <= 0:
    return ""
  diff = spot - prev_close
  sign = "+" if diff > 0 else ""
  return f"({sign}{diff:.2f} pts from Prev Close: {prev_close:.2f})"


def get_pre_market_status():
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
  highs = df_5m["high"].values
  lows = df_5m["low"].values
  n = len(df_5m)

  end_idx = n - 2
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
  atm_strike = int(round(spot / 50.0) * 50)
  exit_msg_block = ""

  if state["active_position"] is not None:
    old_pos = state["active_position"]
    points_captured = (
        (spot - old_pos["entry_spot"])
        if "BULL" in old_pos["type"]
        else (old_pos["entry_spot"] - spot)
    )
    rupee_pnl = points_captured * 0.22 * POSITION_QTY
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
  state = load_state()
  now_ist = datetime.now(IST)
  now_ts = sleep_time.time()
  current_time_str = now_ist.strftime("%I:%M:%S %p IST")
  date_str = now_ist.strftime("%Y-%m-%d")
  hour = now_ist.hour
  minute = now_ist.minute
  target_expiry = get_target_expiry(now_ist)

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
            f"👉 *Plan:* Awaiting regular market open (09:15 AM)."
        )
        send_telegram(msg)

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

  # ==========================================================
  # 1.5 COMBINED EMERGENCY CIRCUIT BREAKER (MATH FLOOR + HARD CAP)
  # ==========================================================
  if state.get("pending_confirmation") is not None and state.get("active_position") is not None:
      trial_pos = state["active_position"]
      backup_pos = state.get("previous_position_backup")
      is_bull_trial = "BULL" in trial_pos["type"]
      entry_spot = trial_pos["entry_spot"]
      live_pnl = (spot - entry_spot) if is_bull_trial else (entry_spot - spot)
      
      hard_stop_hit = live_pnl <= -MAX_TRIAL_LOSS_PTS
      ema_floor_hit = (spot <= s_invalidation) if is_bull_trial else (spot >= s_invalidation)
      
      if hard_stop_hit or ema_floor_hit:
          rupee_loss = live_pnl * 0.22 * POSITION_QTY
          trigger_reason = "HARD CAP HIT" if hard_stop_hit else "EMA FLOOR BREACH"
          
          log_paper_trade({
              "timestamp": current_time_str,
              "action": f"CLOSE_EMERGENCY_{'HARD' if hard_stop_hit else 'MATH'}_LEG2",
              "direction": trial_pos["type"],
              "expiry": trial_pos["expiry"],
              "spot": spot,
              "buy_strike": trial_pos["buy_strike"],
              "sell_strike": trial_pos["sell_strike"],
              "pnl_pts": round(live_pnl, 2),
              "est_net_inr": round(rupee_loss, 2),
          })

          reopened_type = (
              backup_pos["type"]
              if backup_pos
              else ("BEAR_PUT_SPREAD" if is_bull_trial else "BULL_CALL_SPREAD")
          )
          atm_strike = int(round(spot / 50.0) * 50)
          b_strike = atm_strike
          s_strike = atm_strike + SPREAD_WIDTH if "BULL" in reopened_type else atm_strike - SPREAD_WIDTH
          leg1_pnl_pts = backup_pos.get("captured_pnl_pts", 0.0) if backup_pos else 0.0

          state["active_position"] = {
              "type": reopened_type,
              "expiry": target_expiry,
              "buy_strike": b_strike,
              "sell_strike": s_strike,
              "entry_spot": spot,
              "entry_time": current_time_str,
              "parent_leg1_pnl": leg1_pnl_pts,
              "trial_leg2_pnl": round(live_pnl, 2),
              "is_restored_leg3": True,
          }
          state["pending_confirmation"] = None
          state["previous_position_backup"] = None
          state["armed_direction"] = None
          state["swing_pivot"] = None
          save_state(state)

          log_paper_trade({
              "timestamp": current_time_str,
              "action": "OPEN_EMERGENCY_RESTORED_LEG3",
              "direction": reopened_type,
              "expiry": target_expiry,
              "spot": spot,
              "buy_strike": b_strike,
              "sell_strike": s_strike,
              "pnl_pts": "",
              "est_net_inr": "",
          })
          return

  # ==========================================================
  # 4. 03:15 PM - 03:30 PM EOD CLOSING CROSSOVER & BTST/STBT SPREAD EXECUTION
  # ==========================================================
  if hour == 15 and (15 <= minute <= 30):
    closed_ema5 = float(prev_1h["ema_5"])
    closed_ema10 = float(prev_1h["ema_10"])
    
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
            f"🎯 *03:15–03:30 PM EOD FINAL CLOSING CROSSOVER EXECUTED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{exit_block}"
            f"📦 *Position:* `{spread_name}`\n"
            f"📅 *Target Expiry:* `{target_expiry}`\n"
            f"⏰ *Time:* `{current_time_str}` (Market Close Lock @ 3:30 PM)\n"
            f"📍 *Closing Spot:* `{spot:.2f}` {diff_str}\n"
            f"📈 *Final Closed 5 EMA:* `{closed_ema5:.2f}` | *10 EMA:* `{closed_ema10:.2f}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 *Action:* 1-Hour candle finalized at 3:30 PM session close. Opened overnight carry spread (BTST/STBT)."
        )
        send_telegram(eod_cross_msg)
        return

  bull_cross = (e5 > e10) and (prev_ema5 <= prev_ema10)
  bear_cross = (e5 < e10) and (prev_ema5 >= prev_ema10)

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
          execute_spread(
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


def run_live_loop():
  print("🚀 Nifty Live Scanner & Paper Trader Initialized.")
  evaluate_and_notify()


if __name__ == "__main__":
  run_live_loop()
