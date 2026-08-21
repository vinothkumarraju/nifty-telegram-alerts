from datetime import datetime, timedelta, timezone
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

# Indian Standard Time (UTC + 5:30)
IST = timezone(timedelta(hours=5, minutes=30))
# =======================================================

LAST_ALERT_STATE = {
    "last_alert_type": None,
    "last_alert_timestamp": 0,
    "last_cross_state": None,
}

# Fixed: Only locks if started AFTER the :15-:19 window
_init_now = datetime.now(IST)
LAST_HOURLY_DISPATCH_HOUR = _init_now.hour if _init_now.minute >= 20 else -1


def send_telegram(text: str):
  if "YOUR_BOT_TOKEN" in BOT_TOKEN:
    return
  url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
  payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
  try:
    requests.post(url, json=payload, timeout=10)
  except Exception as e:
    print(f"Telegram error: {e}")


def get_live_ema_status():
  df = yf.download(
      tickers="^NSEI",
      period="5d",
      interval="1h",
      progress=False,
      auto_adjust=True,
  )
  if df is None or df.empty or len(df) < 10:
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


def evaluate_and_notify():
  global LAST_ALERT_STATE, LAST_HOURLY_DISPATCH_HOUR
  data = get_live_ema_status()
  if not data:
    return

  spot = data["spot"]
  e5 = data["live_ema5"]
  e10 = data["live_ema10"]
  gap = data["live_gap"]

  now_ist = datetime.now(IST)
  now_ts = sleep_time.time()
  current_time_str = now_ist.strftime("%I:%M:%S %p IST")

  bull_cross = (e5 > e10) and (data["prev_ema5"] <= data["prev_ema10"])
  bear_cross = (e5 < e10) and (data["prev_ema5"] >= data["prev_ema10"])
  tight_gap = gap <= GAP_THRESHOLD
  trend = (
      "Bullish (5 EMA > 10 EMA)" if e5 > e10 else "Bearish (5 EMA < 10 EMA)"
  )

  print(
      f"[{current_time_str}] Spot: {spot:.2f} | 5 EMA: {e5:.2f} | 10 EMA:"
      f" {e10:.2f} | Gap: {gap:.2f} pts"
  )

  # 1. Live Crossover Alert (Instant)
  if bull_cross and LAST_ALERT_STATE["last_cross_state"] != "BULL":
    LAST_ALERT_STATE["last_cross_state"] = "BULL"
    LAST_ALERT_STATE["last_alert_timestamp"] = now_ts
    msg = (
        f"🟢 *LIVE ALERT: NIFTY 1H BULL CROSSOVER*\n\n"
        f"⏰ *Time:* `{now_ist.strftime('%I:%M %p IST')}`\n"
        f"📍 *Spot:* `{spot:.2f}`\n"
        f"📈 *5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}`\n"
        f"📏 *Gap:* `{gap:.2f} pts`\n\n"
        f"👉 *Action:* Check 5m chart for Swing High breakout!"
    )
    send_telegram(msg)

  elif bear_cross and LAST_ALERT_STATE["last_cross_state"] != "BEAR":
    LAST_ALERT_STATE["last_cross_state"] = "BEAR"
    LAST_ALERT_STATE["last_alert_timestamp"] = now_ts
    msg = (
        f"🔴 *LIVE ALERT: NIFTY 1H BEAR CROSSOVER*\n\n"
        f"⏰ *Time:* `{now_ist.strftime('%I:%M %p IST')}`\n"
        f"📍 *Spot:* `{spot:.2f}`\n"
        f"📉 *5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}`\n"
        f"📏 *Gap:* `{gap:.2f} pts`\n\n"
        f"👉 *Action:* Check 5m chart for Swing Low breakout!"
    )
    send_telegram(msg)

  # 2. Tight Gap Warning (<= 5 pts) with 15-min cooldown
  elif tight_gap:
    if (now_ts - LAST_ALERT_STATE["last_alert_timestamp"]) > 900:
      LAST_ALERT_STATE["last_alert_timestamp"] = now_ts
      msg = (
          f"⚠️ *LIVE WARNING: EMA GAP <= 5 PTS*\n\n"
          f"⏰ *Time:* `{now_ist.strftime('%I:%M %p IST')}`\n"
          f"📍 *Spot:* `{spot:.2f}`\n"
          f"📏 *Live Gap:* `{gap:.2f} pts`\n"
          f"🧭 *Direction:* `{trend}`\n\n"
          f"👉 *Status:* Crossover compression active."
      )
      send_telegram(msg)

  # 3. Scheduled Hourly Close Card (Window: :15 to :19 IST & 15:30 to 15:35 post-CAS)
  is_hourly_close = (15 <= now_ist.minute <= 19) and (
      LAST_HOURLY_DISPATCH_HOUR != now_ist.hour
  )
  is_eod_cas = (
      (now_ist.hour == 15)
      and (30 <= now_ist.minute <= 35)
      and (LAST_HOURLY_DISPATCH_HOUR != 99)
  )

  if is_hourly_close or is_eod_cas:
    LAST_HOURLY_DISPATCH_HOUR = 99 if is_eod_cas else now_ist.hour
    title = (
        "📊 *NIFTY 1H: EOD POST-CAS UPDATE*"
        if is_eod_cas
        else "📊 *NIFTY 1H: HOURLY STATUS UPDATE*"
    )
    msg = (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ *Time:* `{now_ist.strftime('%d-%b %I:%M %p IST')}`\n"
        f"📍 *Nifty Spot:* `{spot:.2f}`\n"
        f"📈 *5 EMA:* `{e5:.2f}`\n"
        f"📉 *10 EMA:* `{e10:.2f}`\n"
        f"📏 *EMA Gap:* `{gap:.2f} pts`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧭 *Trend:* `{trend}`\n"
        f"✅ *Status:* Trend continuing."
    )
    send_telegram(msg)


def run_live_loop():
  print("🚀 Starting Live Nifty Scanner (IST Locked)...")
  start_time = sleep_time.time()
  while (sleep_time.time() - start_time) < 10800:
    try:
      evaluate_and_notify()
    except Exception as e:
      print(f"Loop error: {e}")
    sleep_time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
  run_live_loop()
