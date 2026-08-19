from datetime import datetime
import sys
import traceback
import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ==================== CREDENTIALS ====================
BOT_TOKEN = "8898904634:AAFMPluDTeuI_i6aI25xOdyBdYD-E2x9fsw"
CHAT_ID = "7972609109"
# =====================================================


def send_telegram_alert(text: str):
  """Sends notification to Telegram."""
  if "YOUR_BOT_TOKEN" in BOT_TOKEN or "YOUR_CHAT_ID" in CHAT_ID:
    print("⚠️ Telegram token or chat ID is still set to placeholder values.")
    return False

  url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
  payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}

  try:
    res = requests.post(url, json=payload, timeout=15)
    data = res.json()
    if res.status_code == 200 and data.get("ok"):
      print("✅ Telegram notification delivered successfully!")
      return True
    else:
      print(f"❌ Telegram Error: {data.get('description', res.text)}")
      return False
  except Exception as e:
    print(f"❌ Connection error sending to Telegram: {e}")
    return False


def get_nifty_data():
  """Fetches live 1-hour Nifty 50 candle data."""
  print("📡 Downloading Nifty 1-Hour data from Yahoo Finance...")
  try:
    df = yf.download(
        tickers="^NSEI",
        period="1mo",
        interval="1h",
        progress=False,
        auto_adjust=True,
    )
    if df is not None and not df.empty and len(df) >= 10:
      return df
  except Exception as e:
    print(f"⚠️ yf.download error: {e}")

  try:
    t = yf.Ticker("^NSEI")
    df = t.history(period="1mo", interval="1h")
    if df is not None and not df.empty and len(df) >= 10:
      return df
  except Exception as e:
    print(f"⚠️ Ticker history fallback error: {e}")

  return None


def run_scanner():
  print("=" * 60)
  print(f"🚀 NIFTY 1H SCANNER RUNNING AT: {datetime.utcnow()} UTC")
  print("=" * 60)

  # ----------------------------------------------------
  # 🧪 TEST ALERT (Will fire every time you run this)
  # ----------------------------------------------------
  send_telegram_alert(
      "🧪 *TEST ALERT*: Connection verified! Nifty 1H Scanner is live on GitHub"
      " Actions cloud."
  )

  try:
    df = get_nifty_data()

    if df is None or df.empty or len(df) < 5:
      print("ℹ️ Market data feed unavailable or market closed.")
      return

    # Clean column names
    if isinstance(df.columns, pd.MultiIndex):
      df.columns = [col[0].lower() for col in df.columns]
    else:
      df.columns = [col.lower() for col in df.columns]

    if "close" not in df.columns:
      print(
          f"❌ 'close' column missing. Columns found: {list(df.columns)}"
      )
      return

    # Calculate 5 EMA & 10 EMA
    df["ema_5"] = df["close"].ewm(span=5, adjust=False).mean()
    df["ema_10"] = df["close"].ewm(span=10, adjust=False).mean()
    df["ema_gap"] = (df["ema_5"] - df["ema_10"]).abs()

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

    spot = f"{curr['close']:.2f}"
    ema5 = f"{curr['ema_5']:.2f}"
    ema10 = f"{curr['ema_10']:.2f}"
    gap = f"{curr['ema_gap']:.2f}"

    print(
        f"📊 Candle: {candle_time} | Spot: {spot} | 5 EMA: {ema5} | 10 EMA:"
        f" {ema10} | Gap: {gap} pts"
    )

    if bull_cross:
      msg = (
          f"🟢 *NIFTY 1H: BULLISH CROSSOVER*\n\n"
          f"⏰ *Time:* `{candle_time}`\n"
          f"📈 *Spot:* `{spot}`\n"
          f"📊 *5 EMA:* `{ema5}` | *10 EMA:* `{ema10}`\n"
          f"📏 *Gap:* `{gap} pts`\n\n"
          f"👉 *Action:* Check 5m chart for Swing High breakout!"
      )
      send_telegram_alert(msg)

    elif bear_cross:
      msg = (
          f"🔴 *NIFTY 1H: BEARISH CROSSOVER*\n\n"
          f"⏰ *Time:* `{candle_time}`\n"
          f"📉 *Spot:* `{spot}`\n"
          f"📊 *5 EMA:* `{ema5}` | *10 EMA:* `{ema10}`\n"
          f"📏 *Gap:* `{gap} pts`\n\n"
          f"👉 *Action:* Check 5m chart for Swing Low breakout!"
      )
      send_telegram_alert(msg)

    elif tight_gap:
      bias = "Bullish" if curr["ema_5"] > curr["ema_10"] else "Bearish"
      msg = (
          f"⚠️ *NIFTY 1H: TIGHT GAP WARNING*\n\n"
          f"⏰ *Time:* `{candle_time}`\n"
          f"📍 *Spot:* `{spot}`\n"
          f"📏 *Gap:* `{gap} pts` (≤ 5 pts)\n"
          f"🧭 *Active Bias:* `{bias}`\n\n"
          f"👉 *Status:* Crossover imminent. Be ready on 5m chart."
      )
      send_telegram_alert(msg)
    else:
      print(f"ℹ️ No market signal triggered. Current gap: {gap} pts.")

  except Exception as e:
    print(f"❌ Error during execution: {e}")
    traceback.print_exc()


if __name__ == "__main__":
  run_scanner()
