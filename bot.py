import os
import time
import datetime
import requests
import threading
import yfinance as yf
from flask import Flask

# ==========================================
# 1. FLASK APP SETUP
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is live"

# ==========================================
# 2. CONFIG AUR DUMMY FUNCTIONS (Inhe apne actual functions se replace karein)
# ==========================================
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID_HERE"

# CONFIG aur baki variables jo aapke purane code mein the, unhe yahan define zarur karein
# Example: 
# CONFIG = {"^NSEI": {"yahoo_suffix": "", "gap": 50, "strikes": 10, "sigma": 0.15, "lot": 50}}
# index = "^NSEI"
# expiry_str = "2026-05-14" # Sensex ke liye yaad rakhein weekly expiry Thursday hoti hai
# exp_type = "Weekly"
# market_time = "15:30"
# t = 0.01

# def fetch_option_chain(index, expiry_str):
#     ... returns calls, puts ...
# def gamma(spot, strike, t, sigma, lot):
#     ... returns gamma value ...

# ==========================================
# 3. MAIN TRADING LOGIC
# ==========================================
def main():
    message = ""
    
    # Note: Agar multiple index hain to yahan loop lagana hoga (e.g., for index in CONFIG:)
    global t # Assuming t is defined outside, ya phir loop ke andar define karein
    if t <= 0:
        t = 0.01
        
    cfg = CONFIG[index]

    # Get spot price
    suffix = cfg["yahoo_suffix"]
    spot_ticker = yf.Ticker(index + suffix)
    try:
        spot = spot_ticker.history(period="1d")["Close"].iloc[-1]
    except Exception as e:
        print(f"Error fetching spot: {e}")
        return # Skip if spot not found

    # Get option chain
    calls, puts = fetch_option_chain(index, expiry_str)
    if calls is None or puts is None:
        return

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
            ce_ltp[s] = 0; ce_oi[s] = 0
            
        if not put_row.empty:
            pe_ltp[s] = put_row['lastPrice'].values[0]
            pe_oi[s] = put_row['openInterest'].values[0]
        else:
            pe_ltp[s] = 0; pe_oi[s] = 0

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
            # Values ko proper format karna (Rounding aur Thousands 'K' mein)
            ce = f"{ce_ltp.get(s, 0):.0f}"
            pe = f"{pe_ltp.get(s, 0):.0f}"
            
            # Gamma ko 1L se multiply karne ki bajaye directly thousands 'K' me format kiya hai
            g = f"{gamma_data.get(s, 0) * 100000:.0f}" 
            
            # Net GEX ko Thousands (K) mein convert kar diya hai
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
# 4. TIME SCHEDULER LOOP (Indian Standard Time)
# ==========================================
def main_loop():
    while True:
        # Hamesha IST (UTC + 5:30) calculate karega chahe server kahin bhi ho
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
        
        # 2:30 PM to 3:25 PM
        if (now.hour == 14 and now.minute >= 30) or (now.hour == 15 and now.minute <= 25):
            main()
            time.sleep(60) # 1 minute ruk kar wapas chalega
        else:
            # Agar time match nahi hua to 30 seconds wait karke wapas time check karega
            time.sleep(30)

# ==========================================
# 5. EXECUTION BLOCK
# ==========================================
if __name__ == "__main__":
    # 1. Flask server ko alag thread mein start karna (taaki block na ho)
    port = int(os.environ.get("PORT", 10000))
    flask_thread = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False))
    flask_thread.daemon = True
    flask_thread.start()
    print(f"Flask server started on port {port}")

    # 2. Apna main trading bot loop start karna
    print("Bot is waiting for market time (2:30 PM - 3:25 PM IST)...")
    main_loop()
