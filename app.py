# Install required packages
# !pip install flask pyngrok ccxt pandas pandas_ta python-telegram-bot==13.7 requests --quiet

import os
import logging
import sqlite3
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta
import pytz
import ccxt
import pandas_ta as ta
from telegram import Bot
import telegram
import threading
from flask import Flask, render_template, jsonify
import atexit
from pyngrok import ngrok

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.DEBUG,
    handlers=[logging.FileHandler('td_sto.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Flask app setup
app = Flask(__name__)

# Environment variables
BOT_TOKEN = "6342505709:...."
CHAT_ID = "...."
SYMBOL = "BTC/USDT"
TIMEFRAME = "5m"
NGROK_AUTH_TOKEN = "..."
DB_PATH = "td_sto.db"

# Timezone setup
WAT_TZ = pytz.timezone('Africa/Lagos')

# Global state
bot_active = False
bot_lock = threading.Lock()
conn = None
exchange = ccxt.kraken()
latest_signal = None

# SQLite database setup
def setup_database():
    global conn
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT,
                action TEXT,
                symbol TEXT,
                price REAL,
                message TEXT,
                timeframe TEXT
            )
        ''')
        conn.commit()
        logger.info(f"Database initialized at {DB_PATH}")
        print(f"Database initialized at {DB_PATH}")
    except Exception as e:
        logger.error(f"Database setup error: {e}")
        print(f"Database setup error: {e}")
        conn = None

# Fetch price data
def get_simulated_price():
    try:
        ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=1)
        data = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        data['timestamp'] = pd.to_datetime(data['timestamp'], unit='ms').dt.tz_localize('UTC').dt.tz_convert(WAT_TZ)
        logger.debug(f"Fetched price: {data.iloc[-1]['Close']}")
        return data.iloc[-1]
    except Exception as e:
        logger.error(f"Error fetching price: {e}")
        print(f"Error fetching price: {e}")
        return pd.Series({'Close': 0.0})

# Calculate indicators
def add_indicators(df):
    try:
        df['rsi'] = ta.rsi(df['Close'], length=14)
        kdj = ta.kdj(df['High'], df['Low'], df['Close'], length=9, signal=3)
        df['j'] = kdj['J_9_3']
        return df
    except Exception as e:
        logger.error(f"Error calculating indicators: {e}")
        print(f"Error calculating indicators: {e}")
        return df

# Trading bot logic
def trading_bot():
    global bot_active, latest_signal, conn
    setup_database()
    if conn is None:
        logger.error("Database initialization failed. Exiting.")
        print("Database initialization failed.")
        return

    bot = None
    try:
        bot = Bot(token=BOT_TOKEN)
        logger.info("Telegram bot initialized")
        print("Telegram bot initialized")
    except Exception as e:
        logger.error(f"Error initializing Telegram bot: {e}")
        print(f"Error initializing Telegram bot: {e}")

    last_update_id = 0
    df = pd.DataFrame(columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
    df.set_index('timestamp', inplace=True)

    # Wait for activation
    logger.info("Waiting for bot activation via /start or 30s timeout")
    print("Waiting for bot activation...")
    timeout = time.time() + 30
    while not bot_active and time.time() < timeout:
        time.sleep(1)
    if bot_active:
        print("Bot activated via Telegram")
    else:
        bot_active = True
        print("Bot activated after 30s timeout")

    while bot_active:
        try:
            latest_data = get_simulated_price()
            if latest_data['Close'] == 0.0:
                time.sleep(300)
                continue

            new_row = pd.DataFrame({
                'Open': [latest_data['Open']],
                'Close': [latest_data['Close']],
                'High': [latest_data['High']],
                'Low': [latest_data['Low']],
                'Volume': [latest_data['Volume']]
            }, index=[pd.Timestamp.now(tz=WAT_TZ)])
            df = pd.concat([df, new_row]).tail(100)
            df = add_indicators(df)

            j = df['j'].iloc[-1] if not pd.isna(df['j'].iloc[-1]) else 0.0
            action = "buy" if j < -12.00 else "sell" if j > 121.00 else "hold"
            current_price = latest_data['Close']
            signal = {
                'time': datetime.now(WAT_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                'action': action,
                'symbol': SYMBOL,
                'price': float(current_price),
                'message': f"{action.upper()} {SYMBOL} at {current_price:.2f}",
                'timeframe': TIMEFRAME
            }

            with bot_lock:
                store_signal(signal)
                latest_signal = signal
                logger.info(f"Generated signal: {action} at {current_price:.2f}")
                print(f"Signal generated: {action} at {current_price:.2f}")

                if bot and action != "hold":
                    threading.Thread(target=send_telegram_message, args=(signal,), daemon=True).start()

            if bot:
                try:
                    updates = bot.get_updates(offset=last_update_id, timeout=10)
                    for update in updates:
                        if update.message and update.message.text:
                            text = update.message.text.strip()
                            if text == '/start':
                                with bot_lock:
                                    bot_active = True
                                    bot.send_message(update.message.chat.id, "Bot started.")
                                    print("Bot started via Telegram")
                            elif text == '/stop':
                                with bot_lock:
                                    bot_active = False
                                    bot.send_message(update.message.chat.id, "Bot stopped.")
                                    print("Bot stopped via Telegram")
                        last_update_id = update.update_id + 1
                except Exception as e:
                    logger.error(f"Error processing Telegram updates: {e}")
                    print(f"Error processing Telegram updates: {e}")

            time.sleep(300)  # 5-minute interval
        except Exception as e:
            logger.error(f"Error in trading loop: {e}")
            print(f"Error in trading loop: {e}")
            time.sleep(300)

# Telegram message
def send_telegram_message(signal):
    try:
        bot = Bot(token=BOT_TOKEN)
        message = f"Time: {signal['time']}\nAction: {signal['action']}\nPrice: {signal['price']:.2f}\nMessage: {signal['message']}"
        bot.send_message(chat_id=CHAT_ID, text=message)
        logger.info(f"Telegram message sent: {signal['action']}")
        print(f"Telegram message sent: {signal['action']}")
    except Exception as e:
        logger.error(f"Error sending Telegram message: {e}")
        print(f"Error sending Telegram message: {e}")

# Store signal
def store_signal(signal):
    try:
        if conn is None:
            logger.error("Cannot store signal: Database connection is None")
            print("Error: Database connection is None")
            return
        c = conn.cursor()
        c.execute('''
            INSERT INTO trades (time, action, symbol, price, message, timeframe)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (signal['time'], signal['action'], signal['symbol'], signal['price'], signal['message'], signal['timeframe']))
        conn.commit()
        logger.info(f"Stored signal: {signal['action']} at {signal['price']:.2f}")
        print(f"Stored signal: {signal['action']} at {signal['price']:.2f}")
    except Exception as e:
        logger.error(f"Error storing signal: {e}")
        print(f"Error storing signal: {e}")

# Flask routes
@app.route('/')
def index():
    global latest_signal
    try:
        if not os.path.exists('templates/index.html'):
            logger.warning("index.html not found")
            print("Warning: index.html not found")
            return jsonify({"error": "Template not found", "signal": latest_signal})
        c = conn.cursor() if conn else None
        trades = []
        if c:
            c.execute("SELECT time, action, price, message FROM trades ORDER BY time DESC LIMIT 10")
            trades = [dict(zip(['time', 'action', 'price', 'message'], row)) for row in c.fetchall()]
        logger.info(f"Rendering index.html: trades={len(trades)}")
        print(f"Index rendered with {len(trades)} trades")
        return render_template('index.html', signal=latest_signal, trades=trades, status="active" if bot_active else "stopped")
    except Exception as e:
        logger.error(f"Error rendering index: {e}")
        print(f"Error rendering index: {e}")
        return jsonify({"error": str(e), "signal": latest_signal}), 500

@app.route('/trades_history')
def trades_history():
    try:
        if not os.path.exists('templates/trades_history.html'):
            logger.warning("trades_history.html not found")
            print("Warning: trades_history.html not found")
            return jsonify({"error": "Template not found", "trades": []})
        c = conn.cursor() if conn else None
        trades = []
        if c:
            c.execute("SELECT time, action, price, message FROM trades ORDER BY time DESC LIMIT 25")
            trades = [dict(zip(['time', 'action', 'price', 'message'], row)) for row in c.fetchall()]
        logger.info(f"Rendering trades_history.html: trades={len(trades)}")
        print(f"Trades history rendered with {len(trades)} trades")
        return render_template('trades_history.html', trades=trades)
    except Exception as e:
        logger.error(f"Error rendering trades_history: {e}")
        print(f"Error rendering trades_history: {e}")
        return jsonify({"error": str(e), "trades": []}), 500

@app.route('/status')
def status():
    try:
        data = {"status": "active" if bot_active else "stopped", "signal": latest_signal}
        logger.info(f"Status requested: {data['status']}")
        print(f"Status requested: {data['status']}")
        return jsonify(data)
    except Exception as e:
        logger.error(f"Error in status route: {e}")
        print(f"Error in status route: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/trades/last')
def trades_last():
    try:
        if conn is None:
            logger.error("Database connection is None")
            print("Error: Database connection is None")
            return jsonify({"error": "Database not initialized"}), 503
        c = conn.cursor()
        c.execute("SELECT time, action, price, message FROM trades ORDER BY time DESC LIMIT 10")
        trades = [dict(zip(['time', 'action', 'price', 'message'], row)) for row in c.fetchall()]
        logger.info(f"Fetched {len(trades)} trades for /trades/last")
        print(f"Fetched {len(trades)} trades for /trades/last")
        return jsonify(trades)
    except Exception as e:
        logger.error(f"Error fetching last trades: {e}")
        print(f"Error fetching last trades: {e}")
        return jsonify({"error": str(e), "trades": []}), 500

# Cleanup
def cleanup():
    global conn
    if conn:
        conn.close()
        logger.info("Database connection closed")
        print("Database connection closed")
    ngrok.kill()
    logger.info("Ngrok tunnels terminated")
    print("Ngrok tunnels terminated")

atexit.register(cleanup)

# Start bot and Flask
if __name__ == "__main__":
    try:
        bot_thread = threading.Thread(target=trading_bot, daemon=True)
        bot_thread.start()
        logger.info("Trading bot thread started")
        print("Trading bot thread started")
        ngrok.set_auth_token(NGROK_AUTH_TOKEN)
        public_url = ngrok.connect(5000)
        logger.info(f"Ngrok tunnel: {public_url}")
        print(f"Access your app at: {public_url}")
        app.run(host='0.0.0.0', port=5000, debug=False)
    except Exception as e:
        logger.error(f"Failed to start: {e}")
        print(f"Failed to start: {e}")
