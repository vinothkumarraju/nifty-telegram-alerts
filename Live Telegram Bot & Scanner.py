#!/usr/bin/env python3
"""
NIFTY EMA(5/10) Live Telegram Bot & Scanner
==========================================
Fetches 1-minute NIFTY data (^NSEI) from Yahoo Finance, evaluates day-anchored 
hourly candles, runs confirmation filters, tracks price movements, and sends 
scheduled/event-driven Telegram alerts for GitHub Actions or local execution.
"""

import os
import sys
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import requests
import yfinance as yf

# Configuration from Environment Variables or Defaults
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID')

CONFIG = {
    'ticker': '^NSEI',
    'gap_on': False,     'gap_val': 15.0,
    'pts_on': True,      'pts_val': 100.0,
    'close_on': False,   'close_val': 10.0,
    'breakout_on': False, 'breakout_val': 0.0,
    'swing_on': False,   'swing_val': 10.0,   'swing_lookback': 2,
}

K5 = 2 / 6
K10 = 2 / 11
STATE_FILE = 'bot_state.json'

def send_telegram(text):
    if TELEGRAM_BOT_TOKEN == 'YOUR_BOT_TOKEN' or not TELEGRAM_BOT_TOKEN:
        print(f"[Telegram Mock] {text}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'Markdown'}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if not response.ok:
            print(f"Telegram Error: {response.text}")
    except Exception as e:
        print(f"Telegram connection failed: {e}")

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {'last_processed_timestamp': None, 'alerts_sent_today': []}

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Failed to save state: {e}")

def fetch_yahoo_data(ticker='^NSEI'):
    df = yf.download(ticker, period='5d', interval='1m', progress=False)
    if df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index()
    col_mapping = {c: c.lower() for c in df.columns}
    df = df.rename(columns=col_mapping)
    if 'datetime' in df.columns:
        df = df.rename(columns={'datetime': 'timestamp'})
    elif 'date' in df.columns:
        df = df.rename(columns={'date': 'timestamp'})
    df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(None)
    return df.dropna(subset=['timestamp', 'close']).sort_values('timestamp').reset_index(drop=True)

def _compute_candle_structure(ts):
    n = len(ts)
    day = ts.dt.date.values
    day_change = np.empty(n, dtype=bool)
    day_change[0] = True
    day_change[1:] = day[1:] != day[:-1]
    day_start_idx = np.where(day_change)[0]

    ts_ms = ts.values.astype('datetime64[ms]').astype(np.int64)
    anchors_at_start = ts_ms[day_start_idx]
    anchor_positions = np.searchsorted(day_start_idx, np.arange(n), side='right') - 1
    anchor_for_tick = anchors_at_start[anchor_positions]

    bucket = ((ts_ms - anchor_for_tick) // 3600000).astype(np.int64)
    candle_change = np.empty(n, dtype=bool)
    candle_change[0] = True
    candle_change[1:] = day_change[1:] | (bucket[1:] != bucket[:-1])
    candle_idx = np.cumsum(candle_change) - 1

    starts = np.where(candle_change)[0]
    ends = np.append(starts[1:] - 1, n - 1)
    return dict(ts_ms=ts_ms, candle_idx=candle_idx, ends=ends)

def _ema_over_candles(committed_close):
    n_candles = len(committed_close)
    ema5_c = np.zeros(n_candles)
    ema10_c = np.zeros(n_candles)
    ema5_c[0] = committed_close[0]
    ema10_c[0] = committed_close[0]
    for i in range(1, n_candles):
        ema5_c[i] = committed_close[i] * K5 + ema5_c[i - 1] * (1 - K5)
        ema10_c[i] = committed_close[i] * K10 + ema10_c[i - 1] * (1 - K10)
    return ema5_c, ema10_c

def build_hourly_series(ts, close):
    cs = _compute_candle_structure(ts)
    ends = cs['ends']
    candle_times = ts.values[ends]
    candle_close = close[ends]
    ema5, ema10 = _ema_over_candles(candle_close)
    gap = ema5 - ema10
    return pd.DataFrame({
        'time': candle_times, 'close': candle_close, 'ema5': ema5, 'ema10': ema10, 'gap': gap,
        'candle_idx': np.arange(len(candle_times))
    })

def main():
    ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist)
    current_time_str = now_ist.strftime("%H:%M")
    current_date_str = now_ist.strftime("%Y-%m-%d")
    
    state = load_state()
    # Reset alerts list if date changed
    if state.get('date') != current_date_str:
        state = {'date': current_date_str, 'last_processed_timestamp': None, 'alerts_sent_today': []}

    # 1. Scheduled Time Alerts Check
    alerts_schedule = {
        "09:00": "🤖 *Bot Started*: NIFTY Strategy Bot is online and monitoring.",
        "09:11": "📊 *Pre-Market Info*: Analyzing initial market breadth and setup levels.",
        "09:15": "🔔 *Market Open (9:15)*: Trading session has commenced.",
        "10:15": "⏰ *Hourly Update (10:15)*: First hourly candle completed.",
        "11:15": "⏰ *Hourly Update (11:15)*: Mid-morning hourly candle completed.",
        "12:15": "⏰ *Hourly Update (12:15)*: Midday hourly candle completed.",
        "13:15": "⏰ *Hourly Update (13:15)*: Early afternoon hourly candle completed.",
        "14:15": "⏰ *Hourly Update (14:15)*: Late afternoon hourly candle completed.",
        "15:15": "⏰ *Hourly Update (15:15)*: Final hourly candle completed.",
        "15:30": "🏁 *Post-Market Summary (15:30)*: Market closed for the day."
    }

    if current_time_str in alerts_schedule and current_time_str not in state['alerts_sent_today']:
        send_telegram(alerts_schedule[current_time_str])
        state['alerts_sent_today'].append(current_time_str)
        save_state(state)

    # 2. Data Fetch & Event Checks (Flips, Moves, Confirmations, Noise)
    raw = fetch_yahoo_data(CONFIG['ticker'])
    if raw.empty:
        return

    series = build_hourly_series(raw['timestamp'], raw['close'].values.astype(float))
    if len(series) < 2:
        save_state(state)
        return

    # Detect Flips
    gap = series['gap'].values
    prev = gap[:-1]
    cur = gap[1:]
    mask = ((prev < 0) & (cur > 0)) | ((prev > 0) & (cur < 0))
    mask = mask & (prev != 0) & (cur != 0)
    idxs = np.where(mask)[0] + 1

    events = []
    for i in idxs:
        events.append({
            'time': str(series['time'].values[i]),
            'direction': 'Bullish' if gap[i] > 0 else 'Bearish',
            'gap_at_cross': float(gap[i]),
            'entry_price': float(series['close'].values[i])
        })

    if events:
        latest_event = events[-1]
        last_proc = state.get('last_processed_timestamp')
        
        if latest_event['time'] != last_proc:
            # New event detected
            direction = latest_event['direction']
            price = latest_event['entry_price']
            
            # Send Flip Alert
            send_telegram(f"⚡ *EMA Flip Detected*\n- Direction: {direction}\n- Price: {price:.2f}\n- Time: {latest_event['time']}")
            
            # Evaluate confirmation filters (e.g., Min Points Move)
            pts_ok = abs(latest_event['gap_at_cross']) >= CONFIG['gap_val'] if CONFIG['gap_on'] else True
            
            if pts_ok:
                send_telegram(f"✅ *Trade Confirmed*\n- Strategy accepted {direction} entry at {price:.2f}")
            else:
                send_telegram(f"🔇 *Noise Alert*\n- Flip rejected by confirmation filters (Classified as Noise).")
                
            state['last_processed_timestamp'] = latest_event['time']
            save_state(state)

    # Check NIFTY point movements from open or previous ticks
    current_price = float(raw['close'].iloc[-1])
    open_price = float(raw['open'].iloc[0])
    price_diff = current_price - open_price
    
    if abs(price_diff) >= 100 and f"move_100_{current_date_str}" not in state['alerts_sent_today']:
        send_telegram(f"🚨 *NIFTY Move 100+ Alert!*\n- Index has moved by {price_diff:+.2f} points today.\n- Current Price: {current_price:.2f}")
        state['alerts_sent_today'].append(f"move_100_{current_date_str}")
        save_state(state)
    elif abs(price_diff) >= 50 and f"move_50_{current_date_str}" not in state['alerts_sent_today']:
        send_telegram(f"⚠️ *NIFTY Move 50+ Alert*\n- Index has moved by {price_diff:+.2f} points today.\n- Current Price: {current_price:.2f}")
        state['alerts_sent_today'].append(f"move_50_{current_date_str}")
        save_state(state)

if __name__ == '__main__':
    main()
