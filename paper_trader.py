from datetime import datetime, timedelta, timezone
import json
import os
import time as sleep_time
import traceback
import pandas as pd
import requests
import yfinance as yf

# ==================== CONFIGURATION ====================
BOT_TOKEN = "8898904634:AAFMPluDTeuI_i6aI25xOdyBdYD-E2x9fsw"
CHAT_ID = "7972609109"
SCAN_INTERVAL_SECONDS = 120  # Scans every 2 minutes
GAP_THRESHOLD = 5.0  # Points
SPREAD_WIDTH = 200  # ATM Buy + 200 OTM Sell
STATE_FILE = "strategy_state.json"
TRADE_LOG_FILE = "paper_trades.csv"

# Indian Standard Time (UTC + 5:30)
IST = timezone(timedelta(hours=5, minutes=30))
# =======================================================


def get_target_expiry(dt_ist: datetime) -> str:
  today = dt_ist.date()
  try:
    ticker = yf.Ticker("^NSEI")
    available_expiries = ticker.options
    if available_expiries:
      valid_dates = [
          datetime.strptime(exp, "%Y-%m-%d").date()
          for exp in available_expiries
          if datetime.strptime(exp, "%Y-%m-%d").date() >= today
      ]
      valid_dates.sort()
      if valid_dates:
        nearest = valid_dates[0]
        dte = (nearest - today).days
        target = (
            valid_dates[1] if (dte <= 2 and len(valid_dates) > 1) else nearest
        )
        return target.strftime("%d-%b-%Y")
  except Exception as e:
    print(f"Option chain fetch warning: {e}")

  days_to_tuesday = (1 - today.weekday()) % 7
  nearest_tuesday = today + timedelta(days=days_to_tuesday)
  dte = (nearest_tuesday - today).days
  target = (
      nearest_tuesday + timedelta(days=7) if dte <= 2 else nearest_tuesday
  )
  return target.strftime("%d-%b-%Y")


def load_state():
  default_state = {
      "armed_direction": None,
      "swing_pivot": None,
      "armed_candle_time": None,
      "active_position": None,
      "pending_confirmation": None,
      "previous_position_backup": None,
      "last_cross_state": None,
      "last_processed_5m_candle": None,
      "last_verified_1h_candle": None,
  }
  if os.path.exists(STATE_FILE):
    try:
      with open(STATE_FILE, "r") as f:
        return {**default_state, **json.load(f)}
    except Exception:
      return default_state
  return default_state


def save_state(state):
  with open(STATE_FILE, "w") as f:
    json.dump(state, f, indent=2)


def send_telegram(text: str):
  if "YOUR_BOT_TOKEN" in BOT_TOKEN:
    return
  url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
  payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
  try:
    requests.post(url, json=payload, timeout=10)
  except Exception as e:
    print(f"Telegram error: {e}")


def log_paper_trade(trade_data):
  df = pd.DataFrame([trade_data])
  if not os.path.exists(TRADE_LOG_FILE):
    df.to_csv(TRADE_LOG_FILE, index=False)
  else:
    df.to_csv(TRADE_LOG_FILE, mode="a", header=False, index=False)


def fetch_market_data():
  try:
    df_1h = yf.download(
        tickers="^NSEI",
        period="5d",
        interval="1h",
        progress=False,
        auto_adjust=True,
    )
    df_5m = yf.download(
        tickers="^NSEI",
        period="5d",
        interval="5m",
        progress=False,
        auto_adjust=True,
    )
    if df_1h is None or df_1h.empty or len(df_1h) < 10:
      return None, None
    if df_5m is None or df_5m.empty or len(df_5m) < 20:
      return None, None

    for df in [df_1h, df_5m]:
      if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0].lower() for col in df.columns]
      else:
        df.columns = [col.lower() for col in df.columns]

    df_1h["ema_5"] = df_1h["close"].ewm(span=5, adjust=False).mean()
    df_1h["ema_10"] = df_1h["close"].ewm(span=10, adjust=False).mean()
    return df_1h, df_5m
  except Exception as e:
    print(f"Data fetch error: {e}")
    return None, None


def evaluate_strategy():
  state = load_state()
  df_1h, df_5m = fetch_market_data()
  if df_1h is None or df_5m is None:
    return

  now_ist = datetime.now(IST)
  time_str = now_ist.strftime("%I:%M:%S %p IST")
  target_expiry = get_target_expiry(now_ist)

  curr_1h = df_1h.iloc[-1]
  prev_1h = df_1h.iloc[-2]
  spot = float(df_5m["close"].iloc[-1])

  ema5_1h = float(curr_1h["ema_5"])
  ema10_1h = float(curr_1h["ema_10"])
  gap_1h = abs(ema5_1h - ema10_1h)
  prev_ema5 = float(prev_1h["ema_5"])
  prev_ema10 = float(prev_1h["ema_10"])

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

  # 1. Verification on 1H Candle Close (Confirm or Rollback)
  if (
      state["pending_confirmation"] is not None
      and state.get("last_verified_1h_candle") != closed_1h_time
  ):
    pending = state["pending_confirmation"]
    if pending["candle_time"] == closed_1h_time:
      state["last_verified_1h_candle"] = closed_1h_time
      req_direction = pending["expected_direction"]
      closed_ema5 = float(prev_1h["ema_5"])
      closed_ema10 = float(prev_1h["ema_10"])

      confirmed = (
          (req_direction == "BULLISH" and closed_ema5 > closed_ema10)
          or (req_direction == "BEARISH" and closed_ema5 < closed_ema10)
      )

      if confirmed:
        state["pending_confirmation"] = None
        state["previous_position_backup"] = None
        save_state(state)
        msg = (
            f"✅ *1H CANDLE CONFIRMATION VERIFIED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ *1H Candle Closed:* `{closed_1h_time}`\n"
            f"📈 *Closed 5 EMA:* `{closed_ema5:.2f}` | *10 EMA:*"
            f" `{closed_ema10:.2f}`\n"
            f"📦 *Position:* `{state['active_position']['type']}`\n"
            f"📅 *Expiry:* `{state['active_position']['expiry']}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔒 *Status:* Trend confirmed. Position locked."
        )
        send_telegram(msg)
      else:
        failed_pos = state["active_position"]
        backup_pos = state["previous_position_backup"]
        log_paper_trade({
            "timestamp": time_str,
            "action": "CLOSE_INVALIDATED",
            "direction": failed_pos["type"],
            "expiry": failed_pos["expiry"],
            "spot": spot,
            "buy_strike": failed_pos["buy_strike"],
            "sell_strike": failed_pos["sell_strike"],
        })

        reopened_type = (
            backup_pos["type"]
            if backup_pos
            else ("BEAR_PUT_SPREAD" if req_direction == "BULLISH" else "BULL_CALL_SPREAD")
        )
        atm_strike = int(round(spot / 50.0) * 50)
        b_strike = atm_strike
        s_strike = (
            atm_strike + SPREAD_WIDTH
            if "BULL" in reopened_type
            else atm_strike - SPREAD_WIDTH
        )

        state["active_position"] = {
            "type": reopened_type,
            "expiry": target_expiry,
            "buy_strike": b_strike,
            "sell_strike": s_strike,
            "entry_spot": spot,
            "entry_time": time_str,
        }
        state["pending_confirmation"] = None
        state["previous_position_backup"] = None
        state["armed_direction"] = None
        state["swing_pivot"] = None
        save_state(state)

        log_paper_trade({
            "timestamp": time_str,
            "action": "ROLLBACK_RESTORE",
            "direction": reopened_type,
            "expiry": target_expiry,
            "spot": spot,
            "buy_strike": b_strike,
            "sell_strike": s_strike,
        })
        rollback_msg = (
            f"🚨 *1H CROSSOVER INVALIDATED (ROLLBACK TRIGGERED)*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ *1H Candle Closed:* `{closed_1h_time}`\n"
            f"⚠️ *Reason:* 5 EMA failed to hold beyond 10 EMA.\n"
            f"❌ *Closed Failed:* `{failed_pos['type']}`\n"
            f"🔄 *Restored Spread:* `{reopened_type}` ({b_strike} /"
            f" {s_strike})\n"
            f"📅 *Expiry:* `{target_expiry}`\n"
            f"📍 *Spot:* `{spot:.2f}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🛡️ *Action:* Reverted to prior trend alignment."
        )
        send_telegram(rollback_msg)

  # 2. Strict 1H Crossover Detection (Arming Phase)
  bull_cross = (ema5_1h > ema10_1h) and (prev_ema5 <= prev_ema10)
  bear_cross = (ema5_1h < ema10_1h) and (prev_ema5 >= prev_ema10)

  print(
      f"[{time_str}] Spot: {spot:.2f} | 1H 5 EMA: {ema5_1h:.2f} | 10 EMA:"
      f" {ema10_1h:.2f} | Gap: {gap_1h:.2f} pts | Target Expiry:"
      f" {target_expiry}"
  )

  if bull_cross and state["last_cross_state"] != "BULL":
    swing_high = float(df_5m["high"].iloc[-12:-2].max())
    state["armed_direction"] = "BULLISH"
    state["swing_pivot"] = swing_high
    state["armed_candle_time"] = forming_1h_time
    state["last_cross_state"] = "BULL"
    save_state(state)
    msg = (
        f"🎯 *1H BULLISH CROSSOVER DETECTED (ARMED)*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ *Time:* `{time_str}`\n"
        f"📍 *Spot:* `{spot:.2f}`\n"
        f"📈 *1H 5 EMA:* `{ema5_1h:.2f}` | *10 EMA:* `{ema10_1h:.2f}`\n"
        f"🎯 *5m Swing High Target:* `{swing_high:.2f}`\n"
        f"📅 *Target Expiry:* `{target_expiry}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 Waiting for 5m close > `{swing_high:.2f}` AND 1H Gap > 5.0 pts."
    )
    send_telegram(msg)

  elif bear_cross and state["last_cross_state"] != "BEAR":
    swing_low = float(df_5m["low"].iloc[-12:-2].min())
    state["armed_direction"] = "BEARISH"
    state["swing_pivot"] = swing_low
    state["armed_candle_time"] = forming_1h_time
    state["last_cross_state"] = "BEAR"
    save_state(state)
    msg = (
        f"🎯 *1H BEARISH CROSSOVER DETECTED (ARMED)*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ *Time:* `{time_str}`\n"
        f"📍 *Spot:* `{spot:.2f}`\n"
        f"📉 *1H 5 EMA:* `{ema5_1h:.2f}` | *10 EMA:* `{ema10_1h:.2f}`\n"
        f"🎯 *5m Swing Low Target:* `{swing_low:.2f}`\n"
        f"📅 *Target Expiry:* `{target_expiry}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 Waiting for 5m close < `{swing_low:.2f}` AND 1H Gap > 5.0 pts."
    )
    send_telegram(msg)

  # 3. 5M Breakout Verification & 200-Pt Spread Execution
  if state["armed_direction"] is not None and state["swing_pivot"] is not None:
    last_closed_5m = df_5m.iloc[-2]
    candle_close_5m = float(last_closed_5m["close"])
    candle_time_5m = (
        df_5m.index[-2].strftime("%I:%M %p")
        if hasattr(df_5m.index[-2], "strftime")
        else str(df_5m.index[-2])
    )

    if state.get("last_processed_5m_candle") != candle_time_5m:
      state["last_processed_5m_candle"] = candle_time_5m
      direction = state["armed_direction"]
      pivot = float(state["swing_pivot"])

      is_bull_trigger = (
          (direction == "BULLISH")
          and (candle_close_5m > pivot)
          and (gap_1h > GAP_THRESHOLD)
      )
      is_bear_trigger = (
          (direction == "BEARISH")
          and (candle_close_5m < pivot)
          and (gap_1h > GAP_THRESHOLD)
      )

      if is_bull_trigger or is_bear_trigger:
        atm_strike = int(round(spot / 50.0) * 50)
        state["previous_position_backup"] = state.get("active_position")

        exit_msg_block = ""
        if state["active_position"] is not None:
          old_pos = state["active_position"]
          exit_msg_block = (
              f"🔄 *SQUARED OFF PREVIOUS POSITION*\n"
              f"Type: `{old_pos['type']}` | Expiry: `{old_pos['expiry']}`\n"
              f"Strikes: `{old_pos['buy_strike']} / {old_pos['sell_strike']}`\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
          )
          log_paper_trade({
              "timestamp": time_str,
              "action": "CLOSE_FOR_NEW_ENTRY",
              "direction": old_pos["type"],
              "expiry": old_pos["expiry"],
              "spot": spot,
              "buy_strike": old_pos["buy_strike"],
              "sell_strike": old_pos["sell_strike"],
          })

        if is_bull_trigger:
          buy_strike = atm_strike
          sell_strike = atm_strike + SPREAD_WIDTH
          spread_name = f"BULL CALL DEBIT SPREAD ({buy_strike} CE / {sell_strike} CE)"
          trade_type = "BULL_CALL_SPREAD"
        else:
          buy_strike = atm_strike
          sell_strike = atm_strike - SPREAD_WIDTH
          spread_name = f"BEAR PUT DEBIT SPREAD ({buy_strike} PE / {sell_strike} PE)"
          trade_type = "BEAR_PUT_SPREAD"

        state["active_position"] = {
            "type": trade_type,
            "expiry": target_expiry,
            "buy_strike": buy_strike,
            "sell_strike": sell_strike,
            "entry_spot": spot,
            "entry_time": time_str,
        }
        state["pending_confirmation"] = {
            "candle_time": forming_1h_time,
            "expected_direction": direction,
        }
        state["armed_direction"] = None
        state["swing_pivot"] = None
        save_state(state)

        log_paper_trade({
            "timestamp": time_str,
            "action": "OPEN_PENDING_CONFIRMATION",
            "direction": trade_type,
            "expiry": target_expiry,
            "spot": spot,
            "buy_strike": buy_strike,
            "sell_strike": sell_strike,
        })
        exec_msg = (
            f"🚀 *PAPER TRADE EXECUTED (PENDING 1H CLOSE)*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{exit_msg_block}"
            f"📦 *Position:* `{spread_name}`\n"
            f"📅 *Contract Expiry:* `{target_expiry}`\n"
            f"⏰ *Entry Time:* `{time_str}`\n"
            f"📍 *Entry Spot:* `{spot:.2f}`\n"
            f"📊 *5m Breakout Close:* `{candle_close_5m:.2f}`\n"
            f"📏 *1H EMA Gap:* `{gap_1h:.2f} pts` (> 5.0 pts)\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ *Audit Status:* Awaiting 1H candle close confirmation."
        )
        send_telegram(exec_msg)
      else:
        save_state(state)


def run_live_loop():
  now_ist = datetime.now(IST)

  # Weekend blocker (Saturday=5, Sunday=6)
  if now_ist.weekday() >= 5:
    print(
        f"🛑 [{now_ist.strftime('%A, %I:%M %p IST')}] Weekend detected. Market"
        " is closed. Terminating immediately."
    )
    return

  # Off-hours blocker (Before 09:15 AM or After 03:35 PM IST)
  current_minutes = now_ist.hour * 60 + now_ist.minute
  if current_minutes < (9 * 60 + 15) or current_minutes > (15 * 60 + 35):
    print(
        f"🛑 [{now_ist.strftime('%I:%M:%S %p IST')}] Outside NSE market hours"
        " (09:15 AM - 03:35 PM IST). Terminating immediately."
    )
    return

  print(
      "🚀 Starting Nifty Paper Trader Engine (Market Hours Active & Locked)..."
  )
  start_time = sleep_time.time()

  while (sleep_time.time() - start_time) < 10800:
    loop_ist = datetime.now(IST)

    # Auto-Shutdown after 03:35 PM IST (post-CAS)
    if (loop_ist.hour == 15 and loop_ist.minute >= 35) or loop_ist.hour > 15:
      print(
          f"🛑 [{loop_ist.strftime('%I:%M:%S %p IST')}] EOD CAS Complete (3:35"
          " PM). Shutting down engine."
      )
      break

    try:
      evaluate_strategy()
    except Exception as e:
      print(f"Execution error: {e}")
      traceback.print_exc()

    sleep_time.sleep(SCAN_INTERVAL_SECONDS)

  print("🏁 Paper trader session ended.")


if __name__ == "__main__":
  run_live_loop()
