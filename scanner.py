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
SCAN_INTERVAL_SECONDS = 60  # Fast 60-second live market checks
GAP_THRESHOLD = 5.0  # EMA Gap warning threshold (5.0 pts)
SPREAD_WIDTH = 200  # 200-point ATM/OTM debit spread
STATE_FILE = "strategy_state.json"
TRADE_LOG_FILE = "paper_trades.csv"

# Indian Standard Time (UTC + 5:30)
IST = timezone(timedelta(hours=5, minutes=30))
# =======================================================


def get_target_expiry(dt_ist: datetime) -> str:
  """Dynamically fetches active exchange expiry contracts without hardcoded calendars.

  - If days to nearest contract <= 2 days -> Automatically selects next week's
  contract.
  """
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

  # Fallback calculation
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
  with open(STATE_FILE, "w") as f:
    json.dump(state, f, indent=2)


def send_telegram(text: str):
  """Sends formatted Markdown alert cards to Telegram."""
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
    if df_1h is None or df_1h.empty or len(df_1h) < 5:
      return None, None
    if df_5m is None or df_5m.empty or len(df_5m) < 15:
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


def get_pre_market_status():
  try:
    ticker = yf.Ticker("^NSEI")
    daily_df = ticker.history(period="5d", interval="1d")
    if daily_df is None or daily_df.empty or len(daily_df) < 2:
      return None

    prev_close = float(daily_df["Close"].iloc[-2])
    live_df = ticker.history(period="1d", interval="1m")
    current_spot = (
        float(live_df["Close"].iloc[-1])
        if not live_df.empty
        else float(daily_df["Close"].iloc[-1])
    )

    gap_pts = current_spot - prev_close
    gap_pct = (gap_pts / prev_close) * 100.0

    return {
        "prev_close": prev_close,
        "pre_market_spot": current_spot,
        "gap_pts": gap_pts,
        "gap_pct": gap_pct,
    }
  except Exception as e:
    print(f"Pre-market fetch error: {e}")
    return None


def execute_spread(trade_type, spot, target_expiry, time_str, state, reason):
  """Squares off existing opposing spread and opens a new 200-pt debit spread."""
  atm_strike = int(round(spot / 50.0) * 50)
  state["previous_position_backup"] = state.get("active_position")

  exit_msg_block = ""
  if state["active_position"] is not None:
    old_pos = state["active_position"]
    points_captured = (
        (spot - old_pos["entry_spot"])
        if "BULL" in old_pos["type"]
        else (old_pos["entry_spot"] - spot)
    )
    pnl_str = (
        f"+{points_captured:.2f} pts"
        if points_captured > 0
        else f"{points_captured:.2f} pts"
    )

    exit_msg_block = (
        f"🔄 *SQUARED OFF PREVIOUS POSITION*\n"
        f"Type: `{old_pos['type']}` | Expiry: `{old_pos['expiry']}`\n"
        f"Strikes: `{old_pos['buy_strike']} / {old_pos['sell_strike']}`\n"
        f"Captured PnL: `{pnl_str}`\n"
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
        "pnl_pts": round(points_captured, 2),
    })

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
  }

  log_paper_trade({
      "timestamp": time_str,
      "action": f"OPEN_{reason}",
      "direction": trade_type,
      "expiry": target_expiry,
      "spot": spot,
      "buy_strike": buy_strike,
      "sell_strike": sell_strike,
      "pnl_pts": "",
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

  # ==========================================================
  # 1. PRE-MARKET GAP CARD (09:10 AM IST — Window: 09:08 to 09:14)
  # ==========================================================
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
            f"👉 *Plan:* Awaiting regular market open (09:15 AM) for live 1H"
            " crossover & 5m swing verification."
        )
        send_telegram(msg)
        print(f"[{current_time_str}] Dispatched Pre-Market Update.")

  # Fetch 1H and 5M Data
  df_1h, df_5m = fetch_market_data()
  if df_1h is None or df_5m is None:
    return

  spot = float(df_5m["close"].iloc[-1])
  curr_1h = df_1h.iloc[-1]
  prev_1h = df_1h.iloc[-2]

  prev_ema5 = float(prev_1h["ema_5"])
  prev_ema10 = float(prev_1h["ema_10"])

  # Live 1H EMA calculation
  k5 = 2.0 / 6.0
  k10 = 2.0 / 11.0
  e5 = (spot * k5) + (prev_ema5 * (1.0 - k5))
  e10 = (spot * k10) + (prev_ema10 * (1.0 - k10))
  gap = abs(e5 - e10)

  # Calculate exact Crossover Invalidation Floor Level
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
      f"[{current_time_str}] Spot: {spot:.2f} | 5 EMA: {e5:.2f} | 10 EMA:"
      f" {e10:.2f} | Gap: {gap:.2f} pts | Inval Floor: {s_invalidation:.2f}"
  )

  # ==========================================================
  # 2. 1H CANDLE CLOSE AUDIT & ROLLBACK ENGINE (ON NEXT 1H CANDLE)
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
          # 1H Crossover CONFIRMED on close -> Permanently lock position!
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
              " candles."
          )
          send_telegram(msg)
        else:
          # 1H Crossover FAILED on close -> ROLLBACK to previous position!
          failed_pos = state["active_position"]
          backup_pos = state["previous_position_backup"]
          pnl_loss = (
              (spot - failed_pos["entry_spot"])
              if "BULL" in failed_pos["type"]
              else (failed_pos["entry_spot"] - spot)
          )

          log_paper_trade({
              "timestamp": current_time_str,
              "action": "CLOSE_INVALIDATED",
              "direction": failed_pos["type"],
              "expiry": failed_pos["expiry"],
              "spot": spot,
              "buy_strike": failed_pos["buy_strike"],
              "sell_strike": failed_pos["sell_strike"],
              "pnl_pts": round(pnl_loss, 2),
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
              "entry_time": current_time_str,
          }
          state["pending_confirmation"] = None
          state["previous_position_backup"] = None
          state["armed_direction"] = None
          state["swing_pivot"] = None
          save_state(state)

          rollback_msg = (
              f"🚨 *1H CROSSOVER FAILED ON CLOSE — ROLLBACK TRIGGERED*\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
              f"⏰ *1H Candle Closed:* `{closed_1h_time}`\n"
              f"⚠️ *Failure Reason:* 5 EMA failed to hold beyond 10 EMA at"
              " candle close.\n"
              f"❌ *Closed Failed Trade:* `{failed_pos['type']}`\n"
              f"🔄 *Restored Spread:* `{reopened_type}` ({b_strike} /"
              f" {s_strike})\n"
              f"📅 *Target Expiry:* `{target_expiry}`\n"
              f"📍 *Spot:* `{spot:.2f}`\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
              f"🛡️ *Action:* Restored previous trend position to protect"
              " capital."
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
              f"📍 *Spot:* `{spot:.2f}`\n"
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

  # If it is 09:15 AM opening minute and an instant crossover occurs:
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
            f"⏰ *Time:* `{current_time_str}` (Market Open)\n"
            f"📍 *Open Spot:* `{spot:.2f}`\n"
            f"📈 *Live 5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}`\n"
            f"🔒 *Floor Price:* `{s_invalidation:.2f}` (Buffer:"
            f" `{safety_buffer:.2f} pts`)\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 *Reason:* Overnight gap caused instant 1H crossover on opening"
            " tick (bypassed 5m candle wait to avoid slippage)."
        )
        send_telegram(gap_open_msg)
        return

  # ==========================================================
  # 4. 1H CROSSOVER DETECTION & IMMEDIATE 5M PIVOT MARKING
  # ==========================================================
  if bull_cross and state["last_cross_state"] != "BULL":
    swing_high = float(df_5m["high"].iloc[-12:-2].max())
    state["armed_direction"] = "BULLISH"
    state["swing_pivot"] = swing_high
    state["armed_candle_time"] = forming_1h_time
    state["last_cross_state"] = "BULL"
    save_state(state)

    cross_msg = (
        f"🎯 *1H BULLISH CROSSOVER DETECTED (ARMED)*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ *Time:* `{now_ist.strftime('%I:%M %p IST')}`\n"
        f"📍 *Current Spot:* `{spot:.2f}`\n"
        f"📈 *Live 5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}` (Gap:"
        f" `{gap:.2f} pts`)\n"
        f"🔒 *Crossover Invalidation Floor:* `{s_invalidation:.2f}`\n"
        f"🛡️ *Spot Safety Buffer:* `{safety_buffer:.2f} pts`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *Marked 5m Swing High Target:* `{swing_high:.2f}`\n"
        f"📅 *Target Expiry:* `{target_expiry}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 *Next Action:* Watching for 5m candle to fully close >"
        f" `{swing_high:.2f}` to trigger Bull Call Spread."
    )
    send_telegram(cross_msg)

  elif bear_cross and state["last_cross_state"] != "BEAR":
    swing_low = float(df_5m["low"].iloc[-12:-2].min())
    state["armed_direction"] = "BEARISH"
    state["swing_pivot"] = swing_low
    state["armed_candle_time"] = forming_1h_time
    state["last_cross_state"] = "BEAR"
    save_state(state)

    cross_msg = (
        f"🎯 *1H BEARISH CROSSOVER DETECTED (ARMED)*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ *Time:* `{now_ist.strftime('%I:%M %p IST')}`\n"
        f"📍 *Current Spot:* `{spot:.2f}`\n"
        f"📉 *Live 5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}` (Gap:"
        f" `{gap:.2f} pts`)\n"
        f"🔒 *Crossover Invalidation Floor:* `{s_invalidation:.2f}`\n"
        f"🛡️ *Spot Safety Buffer:* `{safety_buffer:.2f} pts`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *Marked 5m Swing Low Target:* `{swing_low:.2f}`\n"
        f"📅 *Target Expiry:* `{target_expiry}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 *Next Action:* Watching for 5m candle to fully close <"
        f" `{swing_low:.2f}` to trigger Bear Put Spread."
    )
    send_telegram(cross_msg)

  # ==========================================================
  # 5. 5M CLOSED CANDLE BREAKOUT CHECK & SPREAD EXECUTION
  # ==========================================================
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

      # 5m candle must FULLY CLOSE beyond pivot
      is_bull_trigger = (direction == "BULLISH") and (candle_close_5m > pivot)
      is_bear_trigger = (direction == "BEARISH") and (candle_close_5m < pivot)

      if is_bull_trigger or is_bear_trigger:
        trade_type = (
            "BULL_CALL_SPREAD" if is_bull_trigger else "BEAR_PUT_SPREAD"
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
              reason="5M_BREAKOUT_CLOSED",
          )

          state["pending_confirmation"] = {
              "candle_time": forming_1h_time,
              "expected_direction": direction,
          }
          state["armed_direction"] = None
          state["swing_pivot"] = None
          save_state(state)

          exec_msg = (
              f"🚀 *TRADE EXECUTED ON 5M CLOSED CANDLE BREAKOUT*\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
              f"{exit_block}"
              f"📦 *Position:* `{spread_name}`\n"
              f"📅 *Expiry:* `{target_expiry}`\n"
              f"⏰ *Executed At:* `{current_time_str}` (5m Bar:"
              f" `{candle_time_5m}`)\n"
              f"📍 *Entry Spot:* `{spot:.2f}`\n"
              f"🎯 *Broken Pivot Level:* `{pivot:.2f}` (5m Close:"
              f" `{candle_close_5m:.2f}`)\n"
              f"🔒 *Floor Price:* `{s_invalidation:.2f}` (Buffer:"
              f" `{safety_buffer:.2f} pts`)\n"
              f"━━━━━━━━━━━━━━━━━━━━━\n"
              f"⏳ *Audit Status:* Awaiting 1H candle close"
              f" (`{forming_1h_time}`) to verify and lock position."
          )
          send_telegram(exec_msg)
        else:
          state["armed_direction"] = None
          state["swing_pivot"] = None
          save_state(state)
      else:
        save_state(state)

  # ==========================================================
  # 6. TIGHT GAP WARNING (<= 5.0 PTS) WITH 15-MIN COOLDOWN
  # ==========================================================
  elif gap <= GAP_THRESHOLD:
    if (now_ts - state.get("last_tight_gap_ts", 0)) > 900:
      state["last_tight_gap_ts"] = now_ts
      save_state(state)
      msg = (
          f"⚠️ *LIVE WARNING: 1H EMA GAP <= 5.0 PTS*\n"
          f"━━━━━━━━━━━━━━━━━━━━━\n"
          f"⏰ *Time:* `{now_ist.strftime('%I:%M %p IST')}`\n"
          f"📍 *Spot:* `{spot:.2f}`\n"
          f"📏 *Live Gap:* `{gap:.2f} pts`\n"
          f"🧭 *Current Alignment:* `{trend}`\n"
          f"━━━━━━━━━━━━━━━━━━━━━\n"
          f"👉 *Status:* EMA compression active. Watching for imminent"
          " crossover."
      )
      send_telegram(msg)

  # ==========================================================
  # 7. SCHEDULED 30-MINUTE STATUS UPDATES & EOD CAS
  # ==========================================================
  target_slot_type = None
  target_slot_id = None

  if 15 <= minute <= 22:
    target_slot_id = f"{date_str}_{hour:02d}_15"
    target_slot_type = "30MIN_STATUS"
  elif (45 <= minute <= 52) and (hour < 15):
    target_slot_id = f"{date_str}_{hour:02d}_45"
    target_slot_type = "30MIN_STATUS"
  elif hour == 15 and (30 <= minute <= 35):
    target_slot_id = f"{date_str}_EOD_CAS"
    target_slot_type = "EOD_CAS"

  if target_slot_id and target_slot_id not in state["dispatched_slots"]:
    state["dispatched_slots"].append(target_slot_id)
    save_state(state)

    if target_slot_type == "EOD_CAS":
      title = "📊 *NIFTY 1H: EOD POST-CAS FINAL UPDATE*"
      status_line = "✅ *Status:* Market Closed & CAS Finalized."
    elif hour == 9 and minute <= 22:
      title = "📊 *NIFTY 1H: MARKET OPEN STATUS (09:15 AM)*"
      status_line = "🚀 *Status:* Regular Session Active."
    elif 15 <= minute <= 22:
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
        f"📍 *Nifty Spot:* `{spot:.2f}`\n"
        f"📈 *Live 5 EMA:* `{e5:.2f}`\n"
        f"📉 *Live 10 EMA:* `{e10:.2f}`\n"
        f"📏 *EMA Gap:* `{gap:.2f} pts`\n"
        f"🔒 *Floor Price:* `{s_invalidation:.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧭 *Trend:* `{trend}`\n"
        f"{status_line}"
    )
    send_telegram(msg)
    print(f"[{current_time_str}] Dispatched Scheduled Update: {target_slot_id}")


def run_live_loop():
  now_ist = datetime.now(IST)

  # Weekend Blocker (Saturday=5, Sunday=6)
  if now_ist.weekday() >= 5:
    print(
        f"🛑 [{now_ist.strftime('%A, %I:%M %p IST')}] Weekend detected. Market"
        " is closed. Terminating."
    )
    return

  # Off-Hours Blocker (09:05 AM to 03:35 PM IST)
  current_minutes = now_ist.hour * 60 + now_ist.minute
  if current_minutes < (9 * 60 + 5) or current_minutes > (15 * 60 + 35):
    print(
        f"🛑 [{now_ist.strftime('%I:%M:%S %p IST')}] Outside trading hours"
        " (09:05 AM - 03:35 PM IST). Terminating."
    )
    return

  print(
      "🚀 Starting Nifty 60-Second Live Scanner, Breakout Engine & 30-Min"
      " Dispatcher..."
  )
  start_time = sleep_time.time()

  while (sleep_time.time() - start_time) < 10800:
    loop_ist = datetime.now(IST)

    if (loop_ist.hour == 15 and loop_ist.minute >= 35) or loop_ist.hour > 15:
      print(
          f"🛑 [{loop_ist.strftime('%I:%M:%S %p IST')}] Market Closed (3:35"
          " PM). Shutting down scanner."
      )
      break

    try:
      evaluate_and_notify()
    except Exception as e:
      print(f"Execution error: {e}")
      traceback.print_exc()

    sleep_time.sleep(SCAN_INTERVAL_SECONDS)

  print("🏁 Scanner session completed successfully.")


if __name__ == "__main__":
  run_live_loop()
