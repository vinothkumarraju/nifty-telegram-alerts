from datetime import datetime, time
import time as sleep_time
import traceback
import pandas as pd
import requests
import yfinance as yf

# ==================== CONFIGURATION ====================
BOT_TOKEN = "8898904634:AAFMPluDTeuI_i6aI25xOdyBdYD-E2x9fsw"
CHAT_ID = "7972609109"
SCAN_INTERVAL_SECONDS = 60  # Checks every 60 seconds during market hours
GAP_THRESHOLD = 5.0  # Alert when gap <= 5 points
# =======================================================

# State tracking to avoid message spamming
LAST_ALERT_STATE = {
    "last_alert_type": None,
    "last_alert_timestamp": 0,
    "last_cross_state": None,
}


def send_telegram(text: str):
  if "YOUR_BOT_TOKEN" in BOT_TOKEN:
    return
  url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
  payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
  try:
    requests.post(url, json=payload, timeout=10)
  except Exception as e:
    print(f"Telegram connection error: {e}")


def get_live_ema_status():
  # Download 1-hour candles
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

  # Closed candle historical series
  df["ema_5"] = df["close"].ewm(span=5, adjust=False).mean()
  df["ema_10"] = df["close"].ewm(span=10, adjust=False).mean()

  # Fetch latest live tick/price
  live_ticker = yf.Ticker("^NSEI")
  live_data = live_ticker.history(period="1d", interval="1m")
  live_spot = (
      float(live_data["Close"].iloc[-1])
      if not live_data.empty
      else float(df["close"].iloc[-1])
  )

  # Previous closed candle EMAs
  prev_ema5 = float(df["ema_5"].iloc[-2])
  prev_ema10 = float(df["ema_10"].iloc[-2])

  # Calculate Live Forming EMAs
  k5 = 2.0 / (5.0 + 1.0)
  k10 = 2.0 / (10.0 + 1.0)

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
      "candle_time": df.index[-1].strftime("%d-%b %I:%M %p"),
  }


def evaluate_and_notify():
  global LAST_ALERT_STATE
  data = get_live_ema_status()
  if not data:
    return

  spot = data["spot"]
  e5 = data["live_ema5"]
  e10 = data["live_ema10"]
  gap = data["live_gap"]
  now_ts = sleep_time.time()
  current_time_str = datetime.now().strftime("%I:%M:%S %p")

  bull_cross = (e5 > e10) and (data["prev_ema5"] <= data["prev_ema10"])
  bear_cross = (e5 < e10) and (data["prev_ema5"] >= data["prev_ema10"])
  tight_gap = gap <= GAP_THRESHOLD

  print(
      f"[{current_time_str}] Spot: {spot:.2f} | 5 EMA: {e5:.2f} | 10 EMA:"
      f" {e10:.2f} | Live Gap: {gap:.2f} pts"
  )

  # 1. Immediate Alert on Crossover
  if bull_cross and LAST_ALERT_STATE["last_cross_state"] != "BULL":
    LAST_ALERT_STATE["last_cross_state"] = "BULL"
    LAST_ALERT_STATE["last_alert_timestamp"] = now_ts
    msg = (
        f"🟢 *LIVE ALERT: NIFTY 1H BULL CROSSOVER*\n\n"
        f"⏰ *Time:* `{current_time_str}` (Intra-candle)\n"
        f"📍 *Live Spot:* `{spot:.2f}`\n"
        f"📈 *Live 5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}`\n"
        f"📏 *Gap:* `{gap:.2f} pts`\n\n"
        f"👉 *Action:* Check 5m chart for Swing High breakout!"
    )
    send_telegram(msg)

  elif bear_cross and LAST_ALERT_STATE["last_cross_state"] != "BEAR":
    LAST_ALERT_STATE["last_cross_state"] = "BEAR"
    LAST_ALERT_STATE["last_alert_timestamp"] = now_ts
    msg = (
        f"🔴 *LIVE ALERT: NIFTY 1H BEAR CROSSOVER*\n\n"
        f"⏰ *Time:* `{current_time_str}` (Intra-candle)\n"
        f"📍 *Live Spot:* `{spot:.2f}`\n"
        f"📉 *Live 5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}`\n"
        f"📏 *Gap:* `{gap:.2f} pts`\n\n"
        f"👉 *Action:* Check 5m chart for Swing Low breakout!"
    )
    send_telegram(msg)

  # 2. Alert on Tight Gap (<= 5 pts) with 15-Minute Cooldown
  elif tight_gap:
    time_since_last = now_ts - LAST_ALERT_STATE["last_alert_timestamp"]
    if time_since_last > 900:  # 900 seconds = 15 mins cooldown
      LAST_ALERT_STATE["last_alert_timestamp"] = now_ts
      trend = "Bullish" if e5 > e10 else "Bearish"
      msg = (
          f"⚠️ *LIVE WARNING: EMA GAP $\\le$ 5 PTS*\n\n"
          f"⏰ *Time:* `{current_time_str}`\n"
          f"📍 *Live Spot:* `{spot:.2f}`\n"
          f"📏 *Live Gap:* `{gap:.2f} pts`\n"
          f"🧭 *Current Direction:* `{trend}`\n\n"
          f"👉 *Status:* EMA compression active. Crossover imminent!"
      )
      send_telegram(msg)


def run_live_loop():
  print("🚀 Starting Live Nifty Gap Scanner (Real-Time Intra-Candle Mode)...")
  # Runs continuously for ~3 hours per GitHub Action trigger session
  start_time = sleep_time.time()
  while (sleep_time.time() - start_time) < 10800:
    try:
      evaluate_and_notify()
    except Exception as e:
      print(f"Error in scan loop: {e}")
    sleep_time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
  run_live_loop()
