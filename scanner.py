from datetime import datetime
import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ----------------- CREDENTIALS -----------------
BOT_TOKEN = 8898904634:AAFMPluDTeuI_i6aI25xOdyBdYD-E2x9fsw
CHAT_ID = 7972609109
# ------------------------------------------------


def send_telegram_alert(text: str):
  url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
  payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
  try:
    res = requests.post(url, json=payload, timeout=10)
    print("Telegram status:", res.status_code)
  except Exception as e:
    print("Telegram error:", e)


def check_nifty_signals():
  print("Fetching 1-hour Nifty OHLC data...")
  df = yf.download(
      "^NSEI", period="5d", interval="1h", progress=False
  ).dropna()

  if isinstance(df.columns, pd.MultiIndex):
    df.columns = [col[0].lower() for col in df.columns]
  else:
    df.columns = [col.lower() for col in df.columns]

  # Calculate 5 EMA and 10 EMA
  df["ema_5"] = df["close"].ewm(span=5, adjust=False).mean()
  df["ema_10"] = df["close"].ewm(span=10, adjust=False).mean()
  df["ema_gap"] = (df["ema_5"] - df["ema_10"]).abs()

  curr = df.iloc[-1]
  prev = df.iloc[-2]
  candle_time = df.index[-1].strftime("%d-%b %I:%M %p")

  bull_cross = (curr["ema_5"] > curr["ema_10"]) and (
      prev["ema_5"] <= prev["ema_10"]
  )
  bear_cross = (curr["ema_5"] < curr["ema_10"]) and (
      prev["ema_5"] >= prev["ema_10"]
  )
  tight_gap = curr["ema_gap"] <= 5.0

  spot = f"{curr['close']:.2f}"
  e5 = f"{curr['ema_5']:.2f}"
  e10 = f"{curr['ema_10']:.2f}"
  gap = f"{curr['ema_gap']:.2f}"

  if bull_cross:
    msg = (
        f"🟢 *NIFTY 1H BULLISH CROSSOVER*\n\n"
        f"⏰ *Candle:* `{candle_time}`\n"
        f"📍 *Spot:* `{spot}`\n"
        f"📊 *5 EMA:* `{e5}` | *10 EMA:* `{e10}`\n"
        f"📏 *Gap:* `{gap} pts`\n\n"
        f"👉 *Action:* Switch to 5m chart -> Check Swing High breakout!"
    )
    send_telegram_alert(msg)

  elif bear_cross:
    msg = (
        f"🔴 *NIFTY 1H BEARISH CROSSOVER*\n\n"
        f"⏰ *Candle:* `{candle_time}`\n"
        f"📍 *Spot:* `{spot}`\n"
        f"📊 *5 EMA:* `{e5}` | *10 EMA:* `{e10}`\n"
        f"📏 *Gap:* `{gap} pts`\n\n"
        f"👉 *Action:* Switch to 5m chart -> Check Swing Low breakout!"
    )
    send_telegram_alert(msg)

  elif tight_gap:
    trend = "Bullish" if curr["ema_5"] > curr["ema_10"] else "Bearish"
    msg = (
        f"⚠️ *NIFTY 1H: TIGHT GAP WARNING*\n\n"
        f"⏰ *Candle:* `{candle_time}`\n"
        f"📍 *Spot:* `{spot}`\n"
        f"📏 *EMA Gap:* `{gap} pts` (≤ 5 pts)\n"
        f"🧭 *Trend:* `{trend}`\n\n"
        f"👉 *Status:* Crossover imminent. Be ready on 5m chart."
    )
    send_telegram_alert(msg)
  else:
    print(f"No alert condition. Current Gap: {gap} pts.")


if __name__ == "__main__":
  check_nifty_signals()
