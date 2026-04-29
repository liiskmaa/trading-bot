import yfinance as yf
import pandas as pd
from ta.momentum import RSIIndicator
import schedule
import time

def check_rsi(symbol='AAPL'):
    df = yf.download(symbol, period='7d', interval='1h')
    if df.empty:
        print(f"No data for {symbol}")
        return

    rsi = RSIIndicator(close=df['Close']).rsi()
    latest_rsi = rsi.iloc[-1]

    print(f"{symbol} RSI: {latest_rsi:.2f}")
    if latest_rsi < 30:
        print("→ Recommendation: BUY (Oversold)")
    elif latest_rsi > 70:
        print("→ Recommendation: SELL (Overbought)")
    else:
        print("→ Recommendation: HOLD")

# Run every hour
schedule.every(1).hours.do(check_rsi)

print("Trading bot started. Press Ctrl+C to stop.")
check_rsi()  # Run immediately

while True:
    schedule.run_pending()
    time.sleep(1)
