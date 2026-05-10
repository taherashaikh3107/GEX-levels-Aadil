import requests
import time
import datetime
import yfinance as yf
from scipy.stats import norm
import math
import calendar

# ============== CONFIG ==============
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"

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
        "weekly_day": 3,      # Thursday
        "monthly_day": 3,     # Thursday
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
# ====================================

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
        if date.weekday() == 3:   # Thursday
            monthly_exp = date
            break
    if today >= monthly_exp:
        # Next month
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
    except:
        return None, None

def gamma(spot, strike, t, sigma, lot=1):
    if spot <= 0 or t <= 0:
        return 0
    d1 = (math.log(spot/strike) + (0.5*sigma*sigma)*t) / (sigma*math.sqrt(t))
    gam = norm.pdf(d1) / (spot * sigma * math.sqrt(t))
    return gam * lot * 100

def main():
    now = datetime.datetime.now()
    today = now.date()
    if today.isoweekday() >= 6 or today.strftime("%Y-%m-%d") in HOLIDAYS_2026:
        return
    # Market hours: 2:30 PM to 3:25 PM
    if not ((now.hour == 14 and now.minute >= 30) or (now.hour == 15 and now.minute <= 25)):
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
        except:
            continue

        # Get option chain
        calls, puts = fetch_option_chain(index, expiry_str)
        if calls is None or puts is None:
            continue

        # Generate strikes around spot
        atm = round(spot / cfg["gap"]) * cfg["gap"]
        strikes = list(range(atm - cfg["strikes"]*cfg["gap"],
                             atm + (cfg["strikes"]+1)*cfg["gap"], cfg["gap"]))

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
            net = (ce_oi.get(s,0)*ce_g) - (pe_oi.get(s,0)*pe_g)
            gamma_data[s] = ce_g
            net_gex_data[s] = net

        # Gamma flip = strike with max net GEX
        flip_strike = max(net_gex_data, key=net_gex_data.get)
        # Call wall = strike with max net GEX (positive), Put wall = min net GEX (negative)
        sorted_strikes = sorted(strikes)
        call_wall = max(sorted_strikes, key=lambda s: net_gex_data.get(s,0))
        put_wall = min(sorted_strikes, key=lambda s: net_gex_data.get(s,0))
        magnet = min(sorted_strikes, key=lambda s: abs(net_gex_data.get(s,0)))

        diff = spot - flip_strike

        # Build table
        table = f"*{index} {exp_type} Expiry*\n"
        table += f"⏰ {market_time}\n"
        table += f"Expiry: {expiry_str}\n"
        table += f"Spot: {spot:.2f} | GammaFlip: {flip_strike}\n"
        table += f"Diff: {diff:.2f} → {'PUT BUY 🔴' if diff < 0 else 'CALL BUY 🟢'}\n\n"
        table += "Strike |CE   |PE   |γ(1L)|NetGEX\n"
        table += "-"*40 + "\n"
        for s in reversed(sorted_strikes):
            ce = f"{ce_ltp.get(s,0):.0f}"
            pe = f"{pe_ltp.get(s,0):.0f}"
            g = f"{gamma_data.get(s,0):.0f}"
            net = net_gex_data.get(s,0) / 100000  # in lakhs
            row = f" {s} | {ce} | {pe} | {g} | {net:+.0f} "
            if s == flip_strike:
                row = f"*{s}* | {ce} | {pe} | {g} | {net:+.0f}*<<"
            table += row + "\n"

        table += f"\n*Call Wall:* {call_wall} (Net GEX: {net_gex_data[call_wall]/100000:.0f}L)\n"
        table += f"*Put Wall:* {put_wall} (Net GEX: {net_gex_data[put_wall]/100000:.0f}L)\n"
        table += f"*Magnet:* {magnet} (Net GEX: {net_gex_data[magnet]/100000:.0f}L)\n\n"

        message += table

    if message:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        try:
            requests.post(url, data=data)
        except:
            pass

if __name__ == "__main__":
    while True:
        now = datetime.datetime.now()
        if ((now.hour == 14 and now.minute >= 30) or
            (now.hour == 15 and now.minute <= 25)):
            main()
            time.sleep(60)
        else:
            next_run = now.replace(hour=14, minute=30, second=0, microsecond=0)
            if now > next_run:
                next_run += datetime.timedelta(days=1)
            sleep_sec = (next_run - now).total_seconds()
            time.sleep(sleep_sec)

import http.server
import socketserver
import os
import threading

def run_web():
    port = int(os.environ.get("PORT", 8000))
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        httpd.serve_forever()

# Web server thread start karo (daemon mode mein)
thread = threading.Thread(target=run_web)
thread.daemon = True
thread.start()
