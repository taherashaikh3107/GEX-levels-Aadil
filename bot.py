import os
import time
import datetime
import requests
import threading
import yfinance as yf
from flask import Flask
from scipy.stats import norm
import math
import calendar

# ==========================================
# 1. FLASK APP SETUP
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is live"

# ==========================================
# 2. CONFIG AND VARIABLES
# ==========================================
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID_HERE"

HOLIDAYS_2026 = [
    "2026-05-28",
    "2026-06-26",
    "2026-09-14",
    "2026-10-02",
    "2026-10-20",
    "2026-11-10",
    "2026-11-24",
    "2026-12-25"
]

CONFIG = {
    "NIFTY": {
        "gap": 100,
        "lot": 65,
        "strikes": 5,
        "sigma": 0.20,
        "weekly_day": 3,
        "monthly_day": 3,
        "yahoo_suffix": ".NS"
    },
    "SENSEX": {
        "gap": 100,
        "lot": 20,
        "strikes": 5,
        "sigma": 0.20,
        "weekly_day": 3,
        "monthly_day": 3,
        "yahoo_suffix": ".BS"
    }
}

# ==========================================
# 3. HELPER FUNCTIONS
# ==========================================
def get_next_expiry(index):
    today = datetime.date.today()
    cfg = CONFIG[index]
    
    # Weekly: next Thursday
    days_ahead = (cfg["weekly_day"] - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    weekly_exp = today + datetime.timedelta(days=days_ahead)

    # Monthly: last Thursday of current month
    month = today.month
    year = today.year
    last_day = calendar.monthrange(year, month)[1]
    for d in range(last_day, 0, -1):
        date = datetime.date(year, month, d)
        if date.weekday() == 3:
            monthly_exp = date
            break
    
    if today >= monthly_exp:
        if month == 12:
            month = 1
            year += 1
        else:
            month += 1
        last_day = calendar.monthrange(year, month)[1]
        for d in range(last_day, 0, -1):
            date = datetime.date(year, month, d)
            if date.weekday() == 3:
                monthly_exp = date
                break

    if weekly_exp < monthly_exp:
        return weekly_exp.strftime("%Y-%m-%d"), "WEEKLY"
    else:
        return monthly_exp.strftime("%Y-%m-%d"), "MONTHLY"

def fetch_option_chain(index, expiry_str):
    suffix = CONFIG[index]["yahoo_suffix"]
    ticker = yf.Ticker(index + suffix)
    try:
        opt = ticker.option_chain(expiry_str)
        return opt.calls, opt.puts
    except Exception as e:
        print(f"Error fetching option chain: {e}")
        return None, None

def gamma(spot, strike, t, sigma, lot=1):
    if spot <= 0 or t <= 0:
        return 0
    d1 = (math.log(spot/strike) + (0.5*sigma*sigma)*t) / (sigma*math.sqrt(t))
    gam = norm.pdf(d1) / (spot * sigma * math.sqrt(t))
    return gam * lot * 100

# ==========================================
# 4. MAIN TRADING LOGIC
# ==========================================
def main():
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    today = now.date()
    
    # Check if market is closed
    if today.isoweekday() >= 6 or today.strftime("%Y-%m-%d") in HOLIDAYS_2026:
        return
    
    market_time = now.strftime("%H:%M")
    message = ""

    for index in ["NIFTY", "SENSEX"]:
        expiry_str, exp_type = get_next_expiry(index)
        expiry_date = datetime.datetime.strptime(expiry_str, "%Y-%m-%d").date()
        t = (expiry_date - today).days / 365.0
        if t <= 0:
            t = 0.01
        
        cfg = CONFIG[index]

        # Get spot price
        suffix = cfg["yahoo_suffix"]
        spot_ticker = yf.Ticker(index + suffix)
        try:
            spot = spot_ticker.history(period="1d")["Close"].iloc[-1]
        except Exception as e:
            print(f"Error fetching spot for {index}: {e}")
            continue

        # Get option chain
        calls, puts = fetch_option_chain(index, expiry_str)
        if calls is None or puts is None:
            continue

        # Generate strikes around spot
        atm = round(spot / cfg["gap"]) * cfg["gap"]
        strikes = list(range(atm - cfg["strikes"] * cfg["gap"],
                             atm + (cfg["strikes"] + 1) * cfg["gap"], cfg["gap"]))

        # Extract data for these strikes
        ce_ltp, pe_ltp = {}, {}
        ce_oi, pe_oi = {}, {}
        for s in strikes:
            call_row = calls[calls['strike'] == s]
            put_row = puts[puts['strike'] == s]
            
            if not call_row.empty:
                ce_ltp[s] = call_row['lastPrice'].values[0]
                ce_oi[s] = call_row['openInterest'].values[0]
            else:
                ce_ltp[s] = 0
                ce_oi[s] = 0
                
            if not put_row.empty:
                pe_ltp[s] = put_row['lastPrice'].values[0]
                pe_oi[s] = put_row['openInterest'].values[0]
            else:
                pe_ltp[s] = 0
                pe_oi[s] = 0

        # Compute gamma and net GEX
        gamma_data = {}
        net_gex_data = {}
        for s in strikes:
            ce_g = gamma(spot, s, t, cfg["sigma"], cfg["lot"])
            pe_g = gamma(spot, s, t, cfg["sigma"], cfg["lot"])
            net = (ce_oi.get(s, 0) * ce_g) - (pe_oi.get(s, 0) * pe_g)
            gamma_data[s] = ce_g
            net_gex_data[s] = net

        # GEX Key Levels Calculation
        if net_gex_data:
            flip_strike = max(net_gex_data, key=net_gex_data.get)
            sorted_strikes = sorted(strikes)
            call_wall = max(sorted_strikes, key=lambda s: net_gex_data.get(s, 0))
            put_wall = min(sorted_strikes, key=lambda s: net_gex_data.get(s, 0))
            magnet = min(sorted_strikes, key=lambda s: abs(net_gex_data.get(s, 0)))

            diff = spot - flip_strike

            # Build table
            table = f"*{index} {exp_type} Expiry*\n"
            table += f"⏰ {market_time}\n"
            table += f"Expiry: {expiry_str}\n"
            table += f"Spot: {spot:.2f} | GammaFlip: {flip_strike}\n"
            table += f"Diff: {diff:.2f} → {'PUT BUY 🔴' if diff < 0 else 'CALL BUY 🟢'}\n\n"
            table += "Strike |CE   |PE   |γ    |NetGEX\n"
            table += "-"*40 + "\n"
            
            for s in reversed(sorted_strikes):
                ce = f"{ce_ltp.get(s, 0):.0f}"
                pe = f"{pe_ltp.get(s, 0):.0f}"
                g = f"{gamma_data.get(s, 0) * 100000:.0f}"
                net_k = net_gex_data.get(s, 0) / 1000
                
                row = f" {s} | {ce} | {pe} | {g} | {net_k:+.0f}K "
                if s == flip_strike:
                    row = f"*{s}* | {ce} | {pe} | {g} | {net_k:+.0f}K *<<"
                table += row + "\n"

            table += f"\n*Call Wall:* {call_wall} (Net GEX: {net_gex_data[call_wall]/1000:.0f}K)\n"
            table += f"*Put Wall:* {put_wall} (Net GEX: {net_gex_data[put_wall]/1000:.0f}K)\n"
            table += f"*Magnet:* {magnet} (Net GEX: {net_gex_data[magnet]/1000:.0f}K)\n\n"

            message += table

    # Send Telegram Message
    if message:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        try:
            requests.post(url, data=data)
        except Exception as e:
            print(f"Telegram sending error: {e}")

# ==========================================
# 5. TIME SCHEDULER LOOP
# ==========================================
def main_loop():
    while True:
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
        
        # 2:30 PM to 3:25 PM IST
        if (now.hour == 14 and now.minute >= 30) or (now.hour == 15 and now.minute <= 25):
            main()
            time.sleep(60)
        else:
            time.sleep(30)

# ==========================================
# 6. EXECUTION BLOCK
# ==========================================
if __name__ == "__main__":
    # Start Flask server in a separate thread
    port = int(os.environ.get("PORT", 10000))
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True
    )
    flask_thread.start()
    print(f"Flask server started on port {port}")

    # Start main trading bot loop
    print("Bot is waiting for market time (2:30 PM - 3:25 PM IST)...")
    main_loop()
