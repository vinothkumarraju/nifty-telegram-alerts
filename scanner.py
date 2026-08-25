from datetime import datetime, timedelta, timezone
import os
import time as sleep_time
import traceback
import pandas as pd
import requests
import yfinance as yf

# ==================== CONFIGURATION ====================
BOT_TOKEN = "8898904634:AAFMPluDTeuI_i6aI25xOdyBdYD-E2x9fsw"
CHAT_ID = "7972609109"
SCAN_INTERVAL_SECONDS = 60  # Updated to 60-second live checks
GAP_THRESHOLD = 5.0  # EMA Gap warning threshold in points

# Indian Standard Time (UTC + 5:30)
IST = timezone(timedelta(hours=5, minutes=30))
# =======================================================

LAST_ALERT_STATE = {
    "last_cross_state": None,
    "last_tight_gap_ts": 0,
    "dispatched_slots": set(),  # Tracks completed 30-min and pre-market slots
}


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


def get_pre_market_status():
  """Fetches Yesterday's Close and Pre-Market Indicative Spot to calculate Gap Up/Down."""
  try:
    ticker = yf.Ticker("^NSEI")
    daily_df = ticker.history(period="5d", interval="1d")
    if daily_df is None or daily_df.empty or len(daily_df) < 2:
      return None

    prev_close = float(daily_df["Close"].iloc[-2])

    # Attempt to fetch pre-market / fast live quote
    live_df = ticker.history(period="1d", interval="1m")
    if not live_df.empty:
      current_spot = float(live_df["Close"].iloc[-1])
    else:
      current_spot = float(daily_df["Close"].iloc[-1])

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


def get_live_ema_status():
  """Fetches 1H history and live 1-minute ticker to calculate live 1H EMAs."""
  try:
    df = yf.download(
        tickers="^NSEI",
        period="5d",
        interval="1h",
        progress=False,
        auto_adjust=True,
    )
    if df is None or df.empty or len(df) < 5:
      return None

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = [col[0].lower() for col in df.columns]
    else:
      df.columns = [col.lower() for col in df.columns]

    df["ema_5"] = df["close"].ewm(span=5, adjust=False).mean()
    df["ema_10"] = df["close"].ewm(span=10, adjust=False).mean()

    live_ticker = yf.Ticker("^NSEI")
    live_data = live_ticker.history(period="1d", interval="1m")
    live_spot = (
        float(live_data["Close"].iloc[-1])
        if not live_data.empty
        else float(df["close"].iloc[-1])
    )

    # Use locked previous candle EMAs
    prev_ema5 = float(df["ema_5"].iloc[-2])
    prev_ema10 = float(df["ema_10"].iloc[-2])

    k5 = 2.0 / 6.0
    k10 = 2.0 / 11.0

    live_ema5 = (live_spot * k5) + (prev_ema5 * (1.0 - k5))
    live_ema10 = (live_spot * k10) + (prev_ema10 * (1.0 - k10))
    live_gap = abs(live_ema5 - live_ema10)

    return {
        "spot": live_spot,
        "live_ema5": live_ema5,
        "live_ema10": live_ema10,
        "live_gap": live_gap,
        "prev_ema5": prev_ema5,
        "prev_ema10": prev_ema10,
    }
  except Exception as e:
    print(f"EMA calculation error: {e}")
    return None


def evaluate_and_notify():
  global LAST_ALERT_STATE
  now_ist = datetime.now(IST)
  now_ts = sleep_time.time()
  current_time_str = now_ist.strftime("%I:%M:%S %p IST")
  date_str = now_ist.strftime("%Y-%m-%d")
  hour = now_ist.hour
  minute = now_ist.minute

  # ==========================================================
  # 1. PRE-MARKET UPDATE (09:10 AM IST — Window: 09:08 to 09:14)
  # ==========================================================
  if hour == 9 and (8 <= minute <= 14):
    slot_id = f"{date_str}_PRE_MARKET"
    if slot_id not in LAST_ALERT_STATE["dispatched_slots"]:
      pre_data = get_pre_market_status()
      if pre_data:
        LAST_ALERT_STATE["dispatched_slots"].add(slot_id)
        gap_pts = pre_data["gap_pts"]
        gap_pct = pre_data["gap_pct"]

        if gap_pts > 15:
          sentiment = "🟢 *GAP UP OPENING EXPECTED*"
        elif gap_pts < -15:
          sentiment = "🔴 *GAP DOWN OPENING EXPECTED*"
        else:
          sentiment = "⚪ *FLAT OPENING EXPECTED*"

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
            " crossover verification."
        )
        send_telegram(msg)
        print(f"[{current_time_str}] Dispatched Pre-Market Update.")

  # Fetch Live Intraday Data
  data = get_live_ema_status()
  if not data:
    return

  spot = data["spot"]
  e5 = data["live_ema5"]
  e10 = data["live_ema10"]
  gap = data["live_gap"]

  bull_cross = (e5 > e10) and (data["prev_ema5"] <= data["prev_ema10"])
  bear_cross = (e5 < e10) and (data["prev_ema5"] >= data["prev_ema10"])
  tight_gap = gap <= GAP_THRESHOLD
  trend = (
      "Bullish (5 EMA > 10 EMA)" if e5 > e10 else "Bearish (5 EMA < 10 EMA)"
  )

  print(
      f"[{current_time_str}] Spot: {spot:.2f} | 5 EMA: {e5:.2f} | 10 EMA:"
      f" {e10:.2f} | Gap: {gap:.2f} pts | Trend: {trend}"
  )

  # ==========================================================
  # 2. INSTANT LIVE CROSSOVER ALERTS
  # ==========================================================
  if bull_cross and LAST_ALERT_STATE["last_cross_state"] != "BULL":
    LAST_ALERT_STATE["last_cross_state"] = "BULL"
    msg = (
        f"🟢 *LIVE ALERT: NIFTY 1H BULL CROSSOVER*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ *Time:* `{now_ist.strftime('%I:%M %p IST')}`\n"
        f"📍 *Spot:* `{spot:.2f}`\n"
        f"📈 *5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}`\n"
        f"📏 *Gap:* `{gap:.2f} pts`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 *Action:* Check 5m chart for Swing High breakout!"
    )
    send_telegram(msg)

  elif bear_cross and LAST_ALERT_STATE["last_cross_state"] != "BEAR":
    LAST_ALERT_STATE["last_cross_state"] = "BEAR"
    msg = (
        f"🔴 *LIVE ALERT: NIFTY 1H BEAR CROSSOVER*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ *Time:* `{now_ist.strftime('%I:%M %p IST')}`\n"
        f"📍 *Spot:* `{spot:.2f}`\n"
        f"📉 *5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}`\n"
        f"📏 *Gap:* `{gap:.2f} pts`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 *Action:* Check 5m chart for Swing Low breakout!"
    )
    send_telegram(msg)

  # ==========================================================
  # 3. TIGHT GAP WARNING (<= 5 PTS) WITH 15-MIN COOLDOWN
  # ==========================================================
  elif tight_gap:
    if (now_ts - LAST_ALERT_STATE["last_tight_gap_ts"]) > 900:
      LAST_ALERT_STATE["last_tight_gap_ts"] = now_ts
      msg = (
          f"⚠️ *LIVE WARNING: EMA GAP <= 5 PTS*\n"
          f"━━━━━━━━━━━━━━━━━━━━━\n"
          f"⏰ *Time:* `{now_ist.strftime('%I:%M %p IST')}`\n"
          f"📍 *Spot:* `{spot:.2f}`\n"
          f"📏 *Live Gap:* `{gap:.2f} pts`\n"
          f"🧭 *Direction:* `{trend}`\n"
          f"━━━━━━━━━━━━━━━━━━━━━\n"
          f"👉 *Status:* Crossover compression active. Possible breakout soon."
      )
      send_telegram(msg)

  # ==========================================================
  # 4. SCHEDULED 30-MINUTE STATUS UPDATES & EOD CAS
  # ==========================================================
  target_slot_type = None
  target_slot_id = None

  # Slot A: :15 Window (09:15 to 09:22, 10:15 to 10:22, ..., 15:15 to 15:22)
  if 15 <= minute <= 22:
    target_slot_id = f"{date_str}_{hour:02d}_15"
    target_slot_type = "30MIN_STATUS"

  # Slot B: :45 Window (09:45 to 09:52, 10:45 to 10:52, ..., 14:45 to 14:52)
  elif (45 <= minute <= 52) and (hour < 15):
    target_slot_id = f"{date_str}_{hour:02d}_45"
    target_slot_type = "30MIN_STATUS"

  # Slot C: EOD CAS Finalization (15:30 to 15:35)
  elif hour == 15 and (30 <= minute <= 35):
    target_slot_id = f"{date_str}_EOD_CAS"
    target_slot_type = "EOD_CAS"

  if (
      target_slot_id
      and target_slot_id not in LAST_ALERT_STATE["dispatched_slots"]
  ):
    LAST_ALERT_STATE["dispatched_slots"].add(target_slot_id)

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

  # Off-Hours Blocker (Before 09:05 AM or After 03:35 PM IST)
  current_minutes = now_ist.hour * 60 + now_ist.minute
  if current_minutes < (9 * 60 + 5) or current_minutes > (15 * 60 + 35):
    print(
        f"🛑 [{now_ist.strftime('%I:%M:%S %p IST')}] Outside active trading"
        " window (09:05 AM - 03:35 PM IST). Terminating."
    )
    return

  print("🚀 Starting Nifty 60-Second Live Scanner & 30-Min Alert Dispatcher...")
  start_time = sleep_time.time()

  while (sleep_time.time() - start_time) < 10800:
    loop_ist = datetime.now(IST)

    # Shutdown after EOD CAS (03:35 PM IST)
    if (loop_ist.hour == 15 and loop_ist.minute >= 35) or loop_ist.hour > 15:
      print(
          f"🛑 [{loop_ist.strftime('%I:%M:%S %p IST')}] Market Closed (3:35"
          " PM). Shutting down scanner."
      )
      break

    try:
      evaluate_and_notify()
    except Exception as e:
      print(f"Loop error: {e}")
      traceback.print_exc()

    sleep_time.sleep(SCAN_INTERVAL_SECONDS)

  print("🏁 Scanner session completed successfully.")


if __name__ == "__main__":
  run_live_loop()
