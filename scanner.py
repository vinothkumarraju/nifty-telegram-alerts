from datetime import datetime
import traceback
import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ----------------- CREDENTIALS -----------------
# Ensure you replace these with your actual details
BOT_TOKEN = 8898904634:AAFMPluDTeuI_i6aI25xOdyBdYD-E2x9fsw
CHAT_ID = 7972609109
# ------------------------------------------------


def send_telegram_alert(text: str):
  """Sends a notification to your Telegram bot."""
  if "YOUR_BOT_TOKEN" in BOT_TOKEN or "YOUR_CHAT_ID" in CHAT_ID:
    print(
        "⚠️ WARNING: Bot Token or Chat ID is not configured with real"
        " credentials."
    )
    return

  url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
  payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
  try:
    res = requests.post(url, json=payload, timeout=15)
    if res.status_code == 200:
      print("✅ Telegram notification sent successfully!")
    else:
      print(f"⚠️ Telegram API Response ({res.status_code}): {res.text}")
  except Exception as e:
    print(f"❌ Failed to reach Telegram: {e}")


def fetch_nifty_data():
  """Fetches 1-hour Nifty 50 data with fallback handling."""
  print("📡 Fetching Nifty 1-hour data from Yahoo Finance...")

  # Method 1: Ticker history (most stable on cloud runners)
  try:
    ticker = yf.Ticker("^NSEI")
    df = ticker.history(period="1mo", interval="1h", auto_adjust=False)
    if df is not None and not df.empty and len(df) >= 10:
      return df
  except Exception as e:
    print(f"Method 1 failed: {e}")

  # Method 2: Standard download fallback
  try:
    df = yf.download("^NSEI", period="1mo", interval="1h", progress=False)
    if df is not None and not df.empty and len(df) >= 10:
      return df
  except Exception as e:
    print(f"Method 2 failed: {e}")

  return None


def run_scanner():
  try:
    df = fetch_nifty_data()

    if df is None or df.empty or len(df) < 5:
      print("⚠️ No data received from market feed (Market closed or weekend).")
      return

    # Flatten MultiIndex columns if present
    if isinstance(df.columns, pd.MultiIndex):
      df.columns = [col[0].lower() for col in df.columns]
    else:
      df.columns = [col.lower() for col in df.columns]

    # Calculate 5 EMA and 10 EMA
    df["ema_5"] = df["close"].ewm(span=5, adjust=False).mean()
    df["ema_10"] = df["close"].ewm(span=10, adjust=False).mean()
    df["ema_gap"] = (df["ema_5"] - df["ema_10"]).abs()

    # Get latest candle and previous candle
    curr = df.iloc[-1]
    prev = df.iloc[-2]

    candle_time = (
        df.index[-1].strftime("%d-%b %I:%M %p")
        if hasattr(df.index[-1], "strftime")
        else str(df.index[-1])
    )

    bull_cross = (curr["ema_5"] > curr["ema_10"]) and (
        prev["ema_5"] <= prev["ema_10"]
    )
    bear_cross = (curr["ema_5"] < curr["ema_10"]) and (
        prev["ema_5"] >= prev["ema_10"]
    )
    tight_gap = curr["ema_gap"] <= 5.0

    spot_str = f"{curr['close']:.2f}"
    ema5_str = f"{curr['ema_5']:.2f}"
    ema10_str = f"{curr['ema_10']:.2f}"
    gap_str = f"{curr['ema_gap']:.2f}"

    print(
        f"📊 Latest 1H Candle ({candle_time}): Close={spot_str}, 5"
        f" EMA={ema5_str}, 10 EMA={ema10_str}, Gap={gap_str} pts"
    )

    if bull_cross:
      msg = (
          f"🟢 *NIFTY 1H: BULLISH CROSSOVER*\n\n"
          f"⏰ *Time:* `{candle_time}`\n"
          f"📈 *Spot:* `{spot_str}`\n"
          f"📊 *5 EMA:* `{ema5_str}` | *10 EMA:* `{ema10_str}`\n"
          f"📏 *Gap:* `{gap_str} pts`\n\n"
          f"👉 *Action:* Switch to 5m chart -> Check Swing High breakout!"
      )
      send_telegram_alert(msg)

    elif bear_cross:
      msg = (
          f"🔴 *NIFTY 1H: BEARISH CROSSOVER*\n\n"
          f"⏰ *Time:* `{candle_time}`\n"
          f"📉 *Spot:* `{spot_str}`\n"
          f"📊 *5 EMA:* `{ema5_str}` | *10 EMA:* `{ema10_str}`\n"
          f"📏 *Gap:* `{gap_str} pts`\n\n"
          f"👉 *Action:* Switch to 5m chart -> Check Swing Low breakout!"
      )
      send_telegram_alert(msg)

    elif tight_gap:
      trend = "Bullish" if curr["ema_5"] > curr["ema_10"] else "Bearish"
      msg = (
          f"⚠️ *NIFTY 1H: TIGHT GAP WARNING*\n\n"
          f"⏰ *Time:* `{candle_time}`\n"
          f"📍 *Spot:* `{spot_str}`\n"
          f"📏 *Gap:* `{gap_str} pts` (≤ 5 pts)\n"
          f"🧭 *Active Bias:* `{trend}`\n\n"
          f"👉 *Status:* Crossover compression building up."
      )
      send_telegram_alert(msg)
    else:
      print(
          f"ℹ️ No signal triggered. Current Gap = {gap_str} pts (5 EMA:"
          f" {ema5_str}, 10 EMA: {ema10_str})"
      )

  except Exception as e:
    print(f"❌ Error during execution: {e}")
    traceback.print_exc()


if __name__ == "__main__":
  run_scanner()
