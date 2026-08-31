from datetime import datetime, timedelta, timezone
import json
import os
import sys
import traceback
import numpy as np
import pandas as pd
import requests

# ==================== STRATEGY CONFIGURATION ====================
BOT_TOKEN = str(os.getenv("BOT_TOKEN") or "").strip()
CHAT_ID = str(os.getenv("CHAT_ID") or "").strip()

SPREAD_WIDTH = 200          # 200-point ATM/OTM Debit Spread[cite: 1]
MIN_EMA_GAP = 2.5           # Minimum EMA separation to arm crossover[cite: 1]
GAP_THRESHOLD = 5.0         # EMA Gap compression alert threshold (< 5.0 pts)
POSITION_QTY = 650          # Position quantity (10 lots @ 65 qty for ₹10L Capital)[cite: 1]
STATE_FILE = "strategy_state.json"
TRADE_LOG_FILE = "paper_trades.csv"

# Indian Standard Time (UTC + 5:30)
IST = timezone(timedelta(hours=5, minutes=30))
# ================================================================


def get_target_expiry(dt_ist: datetime) -> str:
    """Dynamically selects active exchange expiry contract (Tuesday schedule)."""
    today = dt_ist.date()
    days_to_tuesday = (1 - today.weekday()) % 7
    nearest_tuesday = today + timedelta(days=days_to_tuesday)
    dte = (nearest_tuesday - today).days
    target = nearest_tuesday + timedelta(days=7) if dte <= 2 else nearest_tuesday
    return target.strftime("%d-%b-%Y")


def load_state():
    """Loads persistent state across GitHub Actions workflow runs."""
    default_state = {
        "armed_direction": None,
        "swing_pivot": None,
        "pivot_candle_time": None,
        "armed_candle_time": None,
        "active_position": None,
        "pending_confirmation": None,
        "previous_position_backup": None,
        "last_cross_state": None,
        "last_verified_5m_candle": None,
        "last_verified_1h_candle": None,
        "last_traded_1h_candle": None,
        "last_tight_gap_candle": None,
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
    """Saves persistent state to repository file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_telegram(text: str):
    """Sends Markdown alert cards to Telegram."""
    if not BOT_TOKEN or "YOUR_BOT_TOKEN" in BOT_TOKEN:
        print(f"[Telegram Console Output]:\n{text}\n")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            print("Telegram alert dispatched successfully.")
        else:
            print(f"Telegram API warning ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"Telegram dispatch error: {e}")


def send_telegram_error(err_title: str, err_detail: str):
    now_ist = datetime.now(IST)
    msg = (
        f"🚨 *NIFTY BOT RUNTIME ERROR / EXCEPTION*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ *Time:* `{now_ist.strftime('%d-%b %I:%M:%S %p IST')}`\n"
        f"⚠️ *Issue:* `{err_title}`\n"
        f"📋 *Traceback:* `{str(err_detail)[:250]}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔄 *Runner:* Automatic retry on next scheduled cron."
    )
    send_telegram(msg)


def log_paper_trade(trade_data):
    df = pd.DataFrame([trade_data])
    if not os.path.exists(TRADE_LOG_FILE):
        df.to_csv(TRADE_LOG_FILE, index=False)
    else:
        df.to_csv(TRADE_LOG_FILE, mode="a", header=False, index=False)


def fetch_yahoo_chart_direct(symbol="%5ENSEI", interval="5m", range_str="60d"):
    endpoints = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_str}&interval={interval}",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_str}&interval={interval}",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }

    for url in endpoints:
        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if "chart" in data and "result" in data["chart"] and data["chart"]["result"]:
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
    """Fetches 5m data and resamples into true 09:15-anchored 1H candles."""
    df_5m = fetch_yahoo_chart_direct("%5ENSEI", interval="5m", range_str="60d")
    df_daily = fetch_yahoo_chart_direct("%5ENSEI", interval="1d", range_str="10d")

    if df_5m is None or df_5m.empty:
        return None, None, None

    df_1h = df_5m.resample("1h", offset="15min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["close"])

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
        df_daily = fetch_yahoo_chart_direct("%5ENSEI", interval="1d", range_str="5d")
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
        print(f"Pre-market notice: {e}")
        return None


def find_first_swing_pivot(df_5m, direction="BULLISH", max_lookback=30):[cite: 1]
    highs = df_5m["high"].values[cite: 1]
    lows = df_5m["low"].values[cite: 1]
    times = df_5m.index
    n = len(df_5m)[cite: 1]

    if n < 3:[cite: 1]
        t_str = times[-1].strftime("%I:%M %p") if hasattr(times[-1], "strftime") else str(times[-1])
        return (float(highs[-1]) if direction == "BULLISH" else float(lows[-1])), t_str[cite: 1]

    end_idx = n - 2[cite: 1]
    start_idx = max(1, end_idx - max_lookback)[cite: 1]

    if direction == "BULLISH":[cite: 1]
        for k in range(end_idx, start_idx, -1):[cite: 1]
            if (k > 0 and highs[k] >= highs[k - 1]) and (k < n - 1 and highs[k] >= highs[k + 1]):[cite: 1]
                t_str = times[k].strftime("%I:%M %p") if hasattr(times[k], "strftime") else str(times[k])
                return float(highs[k]), t_str[cite: 1]
        for k in range(end_idx, start_idx, -1):[cite: 1]
            if k > 0 and highs[k] >= highs[k - 1]:[cite: 1]
                t_str = times[k].strftime("%I:%M %p") if hasattr(times[k], "strftime") else str(times[k])
                return float(highs[k]), t_str[cite: 1]
        t_str = times[end_idx].strftime("%I:%M %p") if hasattr(times[end_idx], "strftime") else str(times[end_idx])
        return float(highs[end_idx]), t_str[cite: 1]
    else:
        for k in range(end_idx, start_idx, -1):[cite: 1]
            if (k > 0 and lows[k] <= lows[k - 1]) and (k < n - 1 and lows[k] <= lows[k + 1]):[cite: 1]
                t_str = times[k].strftime("%I:%M %p") if hasattr(times[k], "strftime") else str(times[k])
                return float(lows[k]), t_str[cite: 1]
        for k in range(end_idx, start_idx, -1):[cite: 1]
            if k > 0 and lows[k] <= lows[k - 1]:[cite: 1]
                t_str = times[k].strftime("%I:%M %p") if hasattr(times[k], "strftime") else str(times[k])
                return float(lows[k]), t_str[cite: 1]
        t_str = times[end_idx].strftime("%I:%M %p") if hasattr(times[end_idx], "strftime") else str(times[end_idx])
        return float(lows[end_idx]), t_str[cite: 1]


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
        )[cite: 1]
        rupee_pnl = points_captured * 0.22 * POSITION_QTY[cite: 1]
        pnl_str = (
            f"+{points_captured:.2f} pts (~+₹{rupee_pnl:,.0f})"
            if points_captured > 0
            else f"{points_captured:.2f} pts (~-₹{abs(rupee_pnl):,.0f})"
        )

        state["previous_position_backup"] = {[cite: 1]
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
            "action": "CLOSE_FOR_NEW_ENTRY_LEG1",[cite: 1]
            "direction": old_pos["type"],
            "expiry": old_pos["expiry"],
            "spot": spot,
            "buy_strike": old_pos["buy_strike"],
            "sell_strike": old_pos["sell_strike"],
            "pnl_pts": round(points_captured, 2),[cite: 1]
            "est_net_inr": round(rupee_pnl, 2),[cite: 1]
        })
    else:
        state["previous_position_backup"] = None[cite: 1]

    if "BULL" in trade_type:[cite: 1]
        buy_strike = atm_strike
        sell_strike = atm_strike + SPREAD_WIDTH[cite: 1]
        spread_name = f"BULL CALL DEBIT SPREAD ({buy_strike} CE / {sell_strike} CE)"
    else:
        buy_strike = atm_strike
        sell_strike = atm_strike - SPREAD_WIDTH[cite: 1]
        spread_name = f"BEAR PUT DEBIT SPREAD ({buy_strike} PE / {sell_strike} PE)"

    state["active_position"] = {[cite: 1]
        "type": trade_type,
        "expiry": target_expiry,
        "buy_strike": buy_strike,
        "sell_strike": sell_strike,
        "entry_spot": spot,
        "entry_time": time_str,
        "is_restored_leg3": False,[cite: 1]
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
    current_time_str = now_ist.strftime("%I:%M:%S %p IST")
    date_str = now_ist.strftime("%Y-%m-%d")
    hour = now_ist.hour
    minute = now_ist.minute
    target_expiry = get_target_expiry(now_ist)

    # 1. 09:00 AM Morning Market Startup Readiness Alert
    if hour == 9 and (0 <= minute <= 5):
        slot_id = f"{date_str}_0900_STARTUP"
        if slot_id not in state["dispatched_slots"]:
            state["dispatched_slots"].append(slot_id)
            save_state(state)
            startup_msg = (
                f"🟢 *NIFTY BOT RUNNER ONLINE & ACTIVE*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ *Time:* `{now_ist.strftime('%d-%b %I:%M %p IST')}`\n"
                f"⚙️ *Engine Mode:* `5M Candle Close Breakout + 1H Confirmation`\n"
                f"🎯 *Active Expiry:* `{target_expiry}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"👉 *Next Check:* Pre-market gap analysis at 09:10 AM IST."
            )
            send_telegram(startup_msg)

    # 2. 09:10 AM Pre-Market Gap Analysis Alert
    if hour == 9 and (8 <= minute <= 14):
        slot_id = f"{date_str}_0910_PRE_MARKET"
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

    # Fetch live data
    df_1h, df_5m, prev_close = fetch_market_data()
    if df_1h is None or df_1h.empty or df_5m is None or df_5m.empty:
        print("Market data not ready or market offline. Terminating cleanly.")
        return

    spot = float(df_5m["close"].iloc[-1])[cite: 1]
    diff_str = get_prev_close_diff_str(spot, prev_close)

    df_1h_calc = df_1h.copy()
    df_1h_calc.loc[df_1h_calc.index[-1], "close"] = spot[cite: 1]
    df_1h_calc["ema_5"] = df_1h_calc["close"].ewm(span=5, adjust=False).mean()[cite: 1]
    df_1h_calc["ema_10"] = df_1h_calc["close"].ewm(span=10, adjust=False).mean()[cite: 1]

    e5 = float(df_1h_calc["ema_5"].iloc[-1])[cite: 1]
    e10 = float(df_1h_calc["ema_10"].iloc[-1])[cite: 1]
    live_gap = abs(e5 - e10)[cite: 1]

    print(f"[{current_time_str}] Spot: {spot:.2f} | 5 EMA: {e5:.2f} | 10 EMA: {e10:.2f} | Live Gap: {live_gap:.2f} pts")

    if len(df_1h_calc) < 2:[cite: 1]
        return

    prev_1h = df_1h_calc.iloc[-2][cite: 1]
    prev_ema5 = float(prev_1h["ema_5"])[cite: 1]
    prev_ema10 = float(prev_1h["ema_10"])[cite: 1]
    closed_1h_time = str(df_1h_calc.index[-2])[cite: 1]
    forming_1h_time = str(df_1h_calc.index[-1])

    prev2_ema5 = float(df_1h_calc.iloc[-3]["ema_5"]) if len(df_1h_calc) >= 3 else prev_ema5[cite: 1]
    prev2_ema10 = float(df_1h_calc.iloc[-3]["ema_10"]) if len(df_1h_calc) >= 3 else prev_ema10[cite: 1]

    closed_5m_time = str(df_5m.index[-2]) if len(df_5m) >= 2 else str(df_5m.index[-1])[cite: 1]
    latest_5m_close = float(df_5m.iloc[-2]["close"]) if len(df_5m) >= 2 else spot[cite: 1]

    s_invalidation = (27.0 * prev_ema10 - 22.0 * prev_ema5) / 5.0
    safety_buffer = abs(spot - s_invalidation)

    # ==========================================================
    # 3. EMA GAP COMPRESSION ALERT (< 5.0 PTS)
    # ==========================================================
    if live_gap < GAP_THRESHOLD and state.get("last_tight_gap_candle") != forming_1h_time:
        state["last_tight_gap_candle"] = forming_1h_time
        save_state(state)
        gap_alert = (
            f"⚡ *EMA GAP COMPRESSION ALERT (< {GAP_THRESHOLD:.1f} PTS)*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ *Time:* `{current_time_str}`\n"
            f"📍 *Nifty Spot:* `{spot:.2f}` {diff_str}\n"
            f"📈 *Live 5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}`\n"
            f"📏 *Current Gap:* `{live_gap:.2f} pts` (High Compression)\n"
            f"🛡️ *Invalidation Floor:* `{s_invalidation:.2f}` (Buffer: `{safety_buffer:.2f} pts`)\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👀 *Action:* Watch for imminent 1H crossover breakout."
        )
        send_telegram(gap_alert)

    # ==========================================================
    # 4. 1H CANDLE CLOSE AUDIT & CONFIRMATION / ROLLBACK
    # ==========================================================
    if state.get("last_verified_1h_candle") != closed_1h_time:[cite: 1]
        is_1h_bull = prev_ema5 > prev_ema10[cite: 1]
        is_1h_bear = prev_ema5 < prev_ema10[cite: 1]

        fresh_1h_bull_cross = is_1h_bull and (prev2_ema5 <= prev2_ema10)[cite: 1]
        fresh_1h_bear_cross = is_1h_bear and (prev2_ema5 >= prev2_ema10)[cite: 1]

        if state.get("pending_confirmation") is not None and state.get("active_position") is not None:[cite: 1]
            req_dir = state["pending_confirmation"]["expected_direction"][cite: 1]
            confirmed = (req_dir == "BULLISH" and is_1h_bull) or (req_dir == "BEARISH" and is_1h_bear)[cite: 1]

            if confirmed:
                conf_msg = (
                    f"✅ *1-HOUR CANDLE CLOSE CONFIRMED (LEG 2 LOCKED)*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏰ *Candle Closed:* `{closed_1h_time}`\n"
                    f"🧭 *Confirmed Direction:* `{req_dir}`\n"
                    f"📈 *Closed 5 EMA:* `{prev_ema5:.2f}` | *10 EMA:* `{prev_ema10:.2f}`\n"
                    f"📦 *Position:* Active spread validated to hold trend."
                )
                send_telegram(conf_msg)
                state["pending_confirmation"] = None[cite: 1]
                state["previous_position_backup"] = None[cite: 1]
                state["last_verified_1h_candle"] = closed_1h_time[cite: 1]
                save_state(state)
            else:
                act_pos = state["active_position"][cite: 1]
                pnl_loss = (spot - act_pos["entry_spot"]) if "BULL" in act_pos["type"] else (act_pos["entry_spot"] - spot)[cite: 1]
                rupee_loss = pnl_loss * 0.22 * POSITION_QTY[cite: 1]
                backup_pos = state.get("previous_position_backup")[cite: 1]
                reopened_type = backup_pos["type"] if backup_pos else ("BEAR_PUT_SPREAD" if req_dir == "BULLISH" else "BULL_CALL_SPREAD")[cite: 1]
                atm_strike = int(round(spot / 50.0) * 50)
                b_strike = atm_strike
                s_strike = atm_strike + SPREAD_WIDTH if "BULL" in reopened_type else atm_strike - SPREAD_WIDTH
                spread_name = f"{'BULL CALL' if 'BULL' in reopened_type else 'BEAR PUT'} DEBIT SPREAD ({b_strike} / {s_strike})"

                log_paper_trade({
                    "timestamp": current_time_str,
                    "action": "CLOSE_ROLLBACK_INVALIDATED_LEG2",[cite: 1]
                    "direction": act_pos["type"],[cite: 1]
                    "expiry": act_pos["expiry"],
                    "spot": spot,[cite: 1]
                    "buy_strike": act_pos["buy_strike"],
                    "sell_strike": act_pos["sell_strike"],
                    "pnl_pts": round(pnl_loss, 2),[cite: 1]
                    "est_net_inr": round(rupee_loss, 2),[cite: 1]
                })

                rollback_msg = (
                    f"⚠️ *1-HOUR REJECTION WICK — ROLLBACK TRIGGERED*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏰ *Time:* `{current_time_str}`\n"
                    f"❌ *Failed Leg 2:* Cut `{act_pos['type']}` @ `{spot:.2f}` ({pnl_loss:+.2f} pts | ~₹{rupee_loss:,.0f})\n"
                    f"🔄 *Restored Parent:* `{spread_name}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🛡️ *Defense:* Restored primary trend spread to prevent false wick loss."
                )
                send_telegram(rollback_msg)

                state["active_position"] = {[cite: 1]
                    "type": reopened_type,
                    "expiry": target_expiry,
                    "buy_strike": b_strike,
                    "sell_strike": s_strike,
                    "entry_spot": spot,[cite: 1]
                    "entry_time": current_time_str,[cite: 1]
                    "is_restored_leg3": True,[cite: 1]
                }
                state["pending_confirmation"] = None[cite: 1]
                state["previous_position_backup"] = None[cite: 1]
                state["armed_direction"] = None[cite: 1]
                state["swing_pivot"] = None[cite: 1]
                state["last_verified_1h_candle"] = closed_1h_time[cite: 1]
                save_state(state)
                return
        else:
            if fresh_1h_bull_cross or fresh_1h_bear_cross:[cite: 1]
                t_type = "BULL_CALL_SPREAD" if fresh_1h_bull_cross else "BEAR_PUT_SPREAD"[cite: 1]
                if state.get("active_position") is None or state["active_position"]["type"] != t_type:[cite: 1]
                    exit_block, spread_name, b_strike, s_strike = execute_spread(
                        t_type, spot, target_expiry, current_time_str, state, reason="1H_CANDLE_CLOSE_CONFIRMED", prev_close=prev_close[cite: 1]
                    )
                    state["last_cross_state"] = "BULL" if fresh_1h_bull_cross else "BEAR"[cite: 1]
                    state["last_verified_1h_candle"] = closed_1h_time[cite: 1]
                    state["last_traded_1h_candle"] = closed_1h_time[cite: 1]
                    save_state(state)

                    alert_msg = (
                        f"🎯 *NEW 1-HOUR CANDLE CLOSE SPREAD EXECUTED*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"{exit_block}"
                        f"📦 *Position:* `{spread_name}`\n"
                        f"📅 *Target Expiry:* `{target_expiry}`\n"
                        f"⏰ *Time:* `{current_time_str}`\n"
                        f"📍 *Nifty Spot:* `{spot:.2f}` {diff_str}\n"
                        f"📈 *Closed 5 EMA:* `{prev_ema5:.2f}` | *10 EMA:* `{prev_ema10:.2f}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🚀 *Action:* 1H crossover officially confirmed on candle close."
                    )
                    send_telegram(alert_msg)
                    return

            state["last_verified_1h_candle"] = closed_1h_time[cite: 1]
            save_state(state)

    # ==========================================================
    # 5. 09:15 AM OPENING GAP MOMENTUM ENTRY
    # ==========================================================
    bull_cross = (e5 > e10) and (prev_ema5 <= prev_ema10)[cite: 1]
    bear_cross = (e5 < e10) and (prev_ema5 >= prev_ema10)[cite: 1]
    gap_size = abs(spot - prev_close) if prev_close else 0.0[cite: 1]

    if hour == 9 and minute == 15 and (bull_cross or bear_cross):[cite: 1]
        t_type = "BULL_CALL_SPREAD" if bull_cross else "BEAR_PUT_SPREAD"[cite: 1]
        if 15.0 <= gap_size <= 350.0:
            if state.get("active_position") is None or state["active_position"]["type"] != t_type:[cite: 1]
                exit_block, spread_name, b_strike, s_strike = execute_spread(
                    t_type, spot, target_expiry, current_time_str, state, reason="OPEN_GAP_INSTANT", prev_close=prev_close[cite: 1]
                )
                state["pending_confirmation"] = {"expected_direction": "BULLISH" if bull_cross else "BEARISH"}[cite: 1]
                state["last_cross_state"] = "BULL" if bull_cross else "BEAR"[cite: 1]
                state["last_traded_1h_candle"] = closed_1h_time[cite: 1]
                save_state(state)

                gap_msg = (
                    f"🚀 *09:15 AM OPENING GAP MOMENTUM EXECUTED*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{exit_block}"
                    f"📦 *Position:* `{spread_name}`\n"
                    f"📅 *Target Expiry:* `{target_expiry}`\n"
                    f"⏰ *Time:* `{current_time_str}`\n"
                    f"📍 *Spot:* `{spot:.2f}` {diff_str}\n"
                    f"📏 *Opening Gap:* `{gap_size:.2f} pts`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👉 *Action:* Opening gap triggered instant 1H crossover fill."
                )
                send_telegram(gap_msg)
                return

    # ==========================================================
    # 6. 15:30 PM POST-CAS CLOSING TREND & SETTLEMENT ALERT
    # ==========================================================
    if hour == 15 and (30 <= minute <= 40):
        cas_slot = f"{date_str}_1530_POST_CAS"
        if cas_slot not in state["dispatched_slots"]:
            state["dispatched_slots"].append(cas_slot)
            save_state(state)

            final_trend = "🟢 Bullish (5 EMA > 10 EMA)" if e5 > e10 else "🔴 Bearish (5 EMA < 10 EMA)"
            pos_info = (
                f"`{state['active_position']['type']}` ({state['active_position']['buy_strike']}/{state['active_position']['sell_strike']})"
                if state.get("active_position")
                else "`No Active Position (Cash)`"
            )

            cas_msg = (
                f"🏁 *NIFTY 03:30 PM POST-CAS CLOSING TREND SUMMARY*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ *Time:* `{now_ist.strftime('%d-%b %I:%M %p IST')}` (Session Closed)\n"
                f"📍 *Final Settlement Spot:* `{spot:.2f}` {diff_str}\n"
                f"📈 *Closed 5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}`\n"
                f"📏 *Final EMA Gap:* `{live_gap:.2f} pts`\n"
                f"🧭 *EOD Closing Trend:* {final_trend}\n"
                f"📦 *Carried Position:* {pos_info}\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🌙 *Status:* Closing Auction complete. Pausing scanner until 09:00 AM next session."
            )
            send_telegram(cas_msg)
            return

    # ==========================================================
    # 7. INTRADAY 1H CROSSOVER & 5M CANDLE CLOSE BREAKOUT
    # ==========================================================
    if (hour < 15 or (hour == 15 and minute < 15)) and (state.get("last_traded_1h_candle") != closed_1h_time):[cite: 1]
        if bull_cross and state.get("last_cross_state") != "BULL" and live_gap >= MIN_EMA_GAP:[cite: 1]
            pivot, p_time = find_first_swing_pivot(df_5m, direction="BULLISH")[cite: 1]
            state["armed_direction"] = "BULLISH"[cite: 1]
            state["swing_pivot"] = pivot[cite: 1]
            state["pivot_candle_time"] = p_time
            state["last_cross_state"] = "BULL"[cite: 1]
            save_state(state)

            arm_msg = (
                f"⚡ *1H CROSSOVER ARMED — AWAITING 5M CLOSE BREAKOUT*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ *Time:* `{current_time_str}`\n"
                f"🧭 *Armed Direction:* `BULLISH (Call Spread)`\n"
                f"🎯 *5M Swing High Pivot:* `{pivot:.2f}` (formed @ {p_time})\n"
                f"📍 *Live Spot:* `{spot:.2f}` {diff_str}\n"
                f"📈 *Live Gap:* `{live_gap:.2f} pts` (5 EMA: {e5:.2f} > 10 EMA: {e10:.2f})\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"👀 *Next Step:* Awaiting 5M candle close > `{pivot:.2f}`."
            )
            send_telegram(arm_msg)

        elif bear_cross and state.get("last_cross_state") != "BEAR" and live_gap >= MIN_EMA_GAP:[cite: 1]
            pivot, p_time = find_first_swing_pivot(df_5m, direction="BEARISH")[cite: 1]
            state["armed_direction"] = "BEARISH"[cite: 1]
            state["swing_pivot"] = pivot[cite: 1]
            state["pivot_candle_time"] = p_time
            state["last_cross_state"] = "BEAR"[cite: 1]
            save_state(state)

            arm_msg = (
                f"⚡ *1H CROSSOVER ARMED — AWAITING 5M CLOSE BREAKOUT*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ *Time:* `{current_time_str}`\n"
                f"🧭 *Armed Direction:* `BEARISH (Put Spread)`\n"
                f"🎯 *5M Swing Low Pivot:* `{pivot:.2f}` (formed @ {p_time})\n"
                f"📍 *Live Spot:* `{spot:.2f}` {diff_str}\n"
                f"📉 *Live Gap:* `{live_gap:.2f} pts` (5 EMA: {e5:.2f} < 10 EMA: {e10:.2f})\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"👀 *Next Step:* Awaiting 5M candle close < `{pivot:.2f}`."
            )
            send_telegram(arm_msg)

        if state.get("armed_direction") and state.get("swing_pivot") and (state.get("last_verified_5m_candle") != closed_5m_time):[cite: 1]
            direction = state["armed_direction"][cite: 1]
            pivot = float(state["swing_pivot"])[cite: 1]

            is_5m_breakout = (
                (direction == "BULLISH" and latest_5m_close > pivot) or
                (direction == "BEARISH" and latest_5m_close < pivot)
            )[cite: 1]

            if is_5m_breakout:[cite: 1]
                t_type = "BULL_CALL_SPREAD" if direction == "BULLISH" else "BEAR_PUT_SPREAD"[cite: 1]
                if state.get("active_position") is None or state["active_position"]["type"] != t_type:[cite: 1]
                    exit_block, spread_name, b_strike, s_strike = execute_spread(
                        t_type, spot, target_expiry, current_time_str, state, reason="5M_CANDLE_CLOSE_BREAKOUT", prev_close=prev_close[cite: 1]
                    )
                    state["pending_confirmation"] = {"expected_direction": direction}[cite: 1]
                    state["armed_direction"] = None[cite: 1]
                    state["swing_pivot"] = None[cite: 1]
                    state["last_traded_1h_candle"] = closed_1h_time[cite: 1]
                    state["last_verified_5m_candle"] = closed_5m_time[cite: 1]
                    save_state(state)

                    breakout_msg = (
                        f"🚀 *5M CANDLE CLOSE BREAKOUT EXECUTED (LEG 2)*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"{exit_block}"
                        f"📦 *Position:* `{spread_name}`\n"
                        f"📅 *Target Expiry:* `{target_expiry}`\n"
                        f"⏰ *Time:* `{current_time_str}`\n"
                        f"📍 *Spot:* `{spot:.2f}` {diff_str}\n"
                        f"🎯 *Pivot Breached:* `{pivot:.2f}` (5M Bar Closed: `{latest_5m_close:.2f}`)\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⏳ *Audit:* Position active. Awaiting 1H candle close confirmation."
                    )
                    send_telegram(breakout_msg)
                    return

            state["last_verified_5m_candle"] = closed_5m_time[cite: 1]
            save_state(state)

    # ==========================================================
    # 8. ROUTINE STATUS CARDS EVERY :15 AND :45 MINUTES (TILL 15:15)
    # ==========================================================
    if (hour == 9 and minute >= 15) or (10 <= hour <= 14) or (hour == 15 and minute <= 15):
        if minute in [15, 45]:
            status_slot = f"{date_str}_{hour:02d}_{minute:02d}_STATUS"
            if status_slot not in state["dispatched_slots"]:
                state["dispatched_slots"].append(status_slot)
                save_state(state)

                pos_summary = (
                    f"`{state['active_position']['type']}` ({state['active_position']['buy_strike']}/{state['active_position']['sell_strike']})"
                    if state.get("active_position")
                    else "`No Active Position (Cash)`"
                )
                trend_state = "Bullish (5 EMA > 10 EMA)" if e5 > e10 else "Bearish (5 EMA < 10 EMA)"

                status_card = (
                    f"📊 *NIFTY STRATEGY SCANNER UPDATE*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏰ *Time:* `{current_time_str}`\n"
                    f"📍 *Spot:* `{spot:.2f}` {diff_str}\n"
                    f"📈 *Live 5 EMA:* `{e5:.2f}` | *10 EMA:* `{e10:.2f}`\n"
                    f"📏 *EMA Gap:* `{live_gap:.2f} pts` | 🧭 *Trend:* `{trend_state}`\n"
                    f"🛡️ *Invalidation Floor:* `{s_invalidation:.2f}` (Buffer: `{safety_buffer:.2f} pts`)\n"
                    f"📦 *Active Position:* {pos_summary}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ *Engine Status:* Operational & Monitoring."
                )
                send_telegram(status_card)


def run_single_pass():
    """Single execution cycle tailored for GitHub Actions runner."""
    now_ist = datetime.now(IST)
    hour, minute = now_ist.hour, now_ist.minute

    # Check if outside trading hours (Mon-Fri 09:00 AM - 03:40 PM IST)
    if now_ist.weekday() >= 5 or hour < 9 or (hour == 15 and minute > 40) or hour > 15:
        print(f"[{now_ist.strftime('%I:%M:%S %p IST')}] ⏸️ Outside market hours. Single pass completed cleanly.")
        return

    evaluate_and_notify()


if __name__ == "__main__":
    try:
        run_single_pass()
        sys.exit(0)
    except Exception as e:
        err_trace = traceback.format_exc()
        print(f"Workflow Exception: {e}")
        send_telegram_error("GitHub Action Runner Exception", err_trace)
        sys.exit(0)
