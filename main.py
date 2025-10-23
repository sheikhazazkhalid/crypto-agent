import ccxt
import pandas as pd
import numpy as np
import sqlite3
import time
from datetime import datetime, timezone
import urllib.parse
import urllib.request
import json
import traceback
import atexit
import sys

CONFIG = {
    # ==== Exchange / General ====
    'api_key': '',                    # Binance/Bybit API key (optional for dry-run)
    'api_secret': '',                 # Binance/Bybit API secret (optional for dry-run)
    'symbols': ['BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'XRP/USDT', 'SOL/USDT', 'ADA/USDT', 'LINK/USDT', 'AVAX/USDT', 'POL/USDT', 'LTC/USDT', 'DOT/USDT'],
    'timeframe': '5m',                # Candle interval: 5m gives smoother signals than 1m
    'limit': 300,                     # Number of candles to fetch (for indicators)
    'poll_interval': 30,              # Seconds between each data fetch / cycle
    'dry_run': True,                  # True = no real trades, for safe testing

    # ==== Telegram (fill these) ====
    'telegram_token': '',             # e.g. '123456:ABC-DEF...'
    'telegram_chat_id': '',           # e.g. '-1001234567890' or '123456789'

    # ==== Discord (fill this) ====
    'discord_webhook': 'https://discord.com/api/webhooks/1430967851421798611/Lz1C4uPNljUJIWpOL5LDjI3FjnraGhBCrCa4omTSWJ8U-LYYN_0Ve8TEXSVdy30EJmI0',
    'discord_username': 'Azaz#8117',  # Optional custom username for webhook messages

    # ==== Feature toggles (enable/disable indicators & S/R) ====
    'enable_ema': True,
    'enable_macd': True,
    'enable_rsi': True,
    'enable_volume_avg': True,
    'enable_sr': False,                # master toggle for support/resistance calculation
    'enable_support': True,           # allow checking support condition for entries
    'enable_resistance': True,        # allow checking resistance condition for entries

    # ==== Cross options: use cross events instead of single-bar sign checks ====
    'use_ema_cross': True,            # True => require EMA Fast crossing above EMA Slow (bullish cross)
    'use_macd_cross': True,           # True => require MACD crossing above Signal (bullish cross)

    # ==== Risk Management ====
    'trade_allocation': 0.10,         # % of available USDT balance per trade (e.g., 5%)
    'min_trade_usdt': 10.0,           # Minimum USDT per trade (to skip dust orders)
    'stop_loss_pct': 0.01,           # 1% stop loss from entry price
    'take_profit_pct': 0.02,          # 2% take profit target → 1:1 R:R ratio approx.

    # ==== Indicator Config ====
    'ema_fast_span': 12,              # Short-term EMA for trend detection
    'ema_slow_span': 26,              # Long-term EMA for trend confirmation
    'macd_signal_span': 9,            # MACD signal line smoothing period
    'rsi_period': 14,                 # RSI lookback window
    'rsi_buy_threshold': 30,          # Buy if RSI < 30
    'rsi_sell_threshold': 60,         # Exit if RSI > 60 (momentum slowing)

    # ==== Support / Resistance ====
    'sr_mode': 'pivot',               # Options: 'pivot', 'rolling', 'fractal'
    'support_window': 50,             # Lookback window for rolling or fractal SR calc
    'support_buffer_pct': 0.003,      # +0.3% above support → safe entry zone
    'resistance_buffer_pct': 0.003,   # -0.3% below resistance → avoid buying too high

    # ==== Breakout Confirmation (Optional) ====
    'enable_breakout': True,          # Enable breakout validation logic
    'breakout_close_bars': 2,         # Require 2 consecutive closes above resistance
    'breakout_volume_multiplier': 1.5,# Volume must exceed avg_volume * multiplier
    'volume_avg_window': 20,          # Lookback for average volume calculation

    # new: minimum number of enabled checks required to enter (use 2 or 3 to be more permissive)
    'min_checks_to_buy': 3,
}



class MultiPairBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = ccxt.binanceus({
            'apiKey': cfg['api_key'],
            'secret': cfg['api_secret'],
            'options': {'defaultType': 'spot'}
        })
        #self.client.set_sandbox_mode(True)
        self.conn = sqlite3.connect('trades.db', check_same_thread=False)
        self.create_tables()
        self.positions = {symbol: None for symbol in cfg['symbols']}
        # register exit handler to notify on normal exit
        try:
            atexit.register(self.on_exit)
        except Exception:
            pass

    # Telegram helper
    def send_telegram_alert(self, text: str):
        token = self.cfg.get('telegram_token') or ''
        chat_id = self.cfg.get('telegram_chat_id') or ''
        if not token or not chat_id:
            return  # no creds provided
        try:
            base = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({
                'chat_id': chat_id,
                'text': text,
                'parse_mode': 'HTML'
            }).encode()
            req = urllib.request.Request(base, data=data)
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as e:
            print(f"[Telegram error] {e}")

    # Discord helper
    def send_discord_alert(self, text: str):
        webhook = self.cfg.get('discord_webhook') or ''
        if not webhook:
            return
        try:
            # include optional username/avatar from config
            payload_dict = {"content": text}
            username = self.cfg.get('discord_username')
            avatar = self.cfg.get('discord_avatar_url')
            if username:
                payload_dict['username'] = username
            if avatar:
                payload_dict['avatar_url'] = avatar

            payload = json.dumps(payload_dict).encode()
            headers = {
                'Content-Type': 'application/json',
                'User-Agent': 'crypto-agent/1.0'
            }
            req = urllib.request.Request(webhook, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode(errors='ignore')
            except Exception:
                body = '<no body>'
            print(f"[Discord HTTPError] code={e.code} reason={e.reason} body={body}")
        except Exception as e:
            print(f"[Discord error] {e}")

    # Convenience to send both (if configured)
    def send_alerts(self, text: str):
        # run both; failures are printed inside helpers
        self.send_telegram_alert(text)
        self.send_discord_alert(text)

    def create_tables(self):
        """
        Create trades table or add missing columns if table exists (simple migration).
        """
        desired_columns = {
            'symbol': 'TEXT',
            'side': 'TEXT',
            'price': 'REAL',
            'amount': 'REAL',
            'stop_loss': 'REAL',
            'take_profit': 'REAL',
            'closed': 'INTEGER DEFAULT 0',
            'profit_loss': 'REAL',
            'timestamp': "DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))"
        }
        with self.conn:
            cur = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'")
            if not cur.fetchone():
                # create fresh table with full schema
                self.conn.execute('''CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    side TEXT,
                    price REAL,
                    amount REAL,
                    stop_loss REAL,
                    take_profit REAL,
                    closed INTEGER DEFAULT 0,
                    profit_loss REAL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
                return

            # table exists -> inspect existing columns and add missing ones
            existing = [row[1] for row in self.conn.execute("PRAGMA table_info('trades')").fetchall()]
            for col, coltype in desired_columns.items():
                if col not in existing:
                    try:
                        self.conn.execute(f'ALTER TABLE trades ADD COLUMN {col} {coltype}')
                    except Exception as e:
                        # log but continue; ALTER failures should not stop bot
                        print(f"[DB migration] failed to add column {col}: {e}")

    def fetch_data(self, symbol):
        ohlcv = self.client.fetch_ohlcv(symbol, self.cfg['timeframe'], limit=self.cfg['limit'])
        df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','volume'])
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        df.set_index('time', inplace=False)
        return df

    def compute_rsi(self, prices, period):
        delta = prices.diff()
        up = delta.where(delta > 0, 0.0)
        down = -delta.where(delta < 0, 0.0)

        # Wilder smoothing: alpha = 1/period, adjust=False
        avg_up = up.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
        avg_down = down.ewm(alpha=1/period, adjust=False, min_periods=period).mean()

        rs = avg_up / avg_down.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50)

    def calculate_indicators(self, df):
        c = self.cfg
        df = df.copy()

        # safe helpers
        def safe_ewm(series, span):
            return series.ewm(span=span, adjust=False).mean()

        # EMA
        if c.get('enable_ema', True):
            df['EMA_fast'] = safe_ewm(df['close'], c['ema_fast_span'])
            df['EMA_slow'] = safe_ewm(df['close'], c['ema_slow_span'])
        else:
            df['EMA_fast'] = np.nan
            df['EMA_slow'] = np.nan

        # MACD (depends on EMAs)
        if c.get('enable_macd', True) and c.get('enable_ema', True):
            df['MACD'] = df['EMA_fast'] - df['EMA_slow']
            df['Signal'] = df['MACD'].ewm(span=c['macd_signal_span'], adjust=False).mean()
        else:
            df['MACD'] = np.nan
            df['Signal'] = np.nan

        # RSI
        if c.get('enable_rsi', True):
            df['RSI'] = self.compute_rsi(df['close'], c['rsi_period'])
        else:
            df['RSI'] = np.nan

        # Volume average
        if c.get('enable_volume_avg', True):
            df['vol_avg'] = df['volume'].rolling(window=c['volume_avg_window']).mean()
        else:
            df['vol_avg'] = np.nan

        # Support / Resistance calculation (master toggle)
        if c.get('enable_sr', True):
            mode = c.get('sr_mode', 'rolling').lower()
            if mode == 'pivot':
                df['Pivot'] = (df['high'] + df['low'] + df['close']) / 3
                df['Support'] = (2 * df['Pivot']) - df['high']
                df['Resistance'] = (2 * df['Pivot']) - df['low']
                df['Support'] = df['Support'].rolling(window=3, min_periods=1).mean()
                df['Resistance'] = df['Resistance'].rolling(window=3, min_periods=1).mean()
            elif mode == 'fractal':
                df['Support'] = df['low'].rolling(window=5, center=True).apply(
                    lambda x: x[2] if x[2] == x.min() else np.nan, raw=True)
                df['Resistance'] = df['high'].rolling(window=5, center=True).apply(
                    lambda x: x[2] if x[2] == x.max() else np.nan, raw=True)
                df['Support'] = df['Support'].ffill().bfill()
                df['Resistance'] = df['Resistance'].ffill().bfill()
            else:
                df['Support'] = df['low'].rolling(window=c['support_window'], min_periods=1).min()
                df['Resistance'] = df['high'].rolling(window=c['support_window'], min_periods=1).max()

            df['Support'] = df['Support'].ffill().bfill()
            df['Resistance'] = df['Resistance'].ffill().bfill()
        else:
            df['Support'] = np.nan
            df['Resistance'] = np.nan

        return df

    def get_balance(self):
        if self.cfg['dry_run']:
            return {'USDT': 1000.0}
        balance = self.client.fetch_balance()
        # some ccxt versions return 'total' or 'free'; choose 'free' if present
        free = balance.get('free', {})
        usdt = free.get('USDT', free.get('USD', 0.0))
        return {'USDT': usdt}

    def place_order(self, symbol, side, amount, price):
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        if self.cfg['dry_run']:
            msg = f"[DRY RUN] {timestamp} | {symbol} | {side.upper()} {amount:.8f} @ {price:.8f}"
            print(msg)
            # send telegram + discord alert for simulation too
            self.send_alerts(f"🟡 {msg}")
            return {'symbol': symbol, 'side': side, 'price': price, 'amount': amount}
        else:
            # market order (price not used by API, kept for logging)
            if side.lower() == 'buy':
                order = self.client.create_market_buy_order(symbol, amount)
            else:
                order = self.client.create_market_sell_order(symbol, amount)
            # try to extract price/amount for message (fallback to provided)
            executed_price = float(order.get('price', price)) if isinstance(order, dict) else price
            executed_amount = float(order.get('amount', amount)) if isinstance(order, dict) else amount
            msg = f"[EXECUTED] {timestamp} | {symbol} | {side.upper()} {executed_amount:.8f} @ {executed_price:.8f}"
            print(msg)
            self.send_alerts(f"✅ {msg}")
            return order

    def log_trade(self, symbol, order, sl, tp):
        with self.conn:
            cur = self.conn.execute(
                '''INSERT INTO trades (symbol, side, price, amount, stop_loss, take_profit, profit_loss)
                   VALUES (?,?,?,?,?,?,?)''',
                (symbol, order['side'], float(order['price']), float(order['amount']), float(sl), float(tp), 0.0)
            )
            return cur.lastrowid

    def close_trade_db(self, trade_id, pnl):
        with self.conn:
            self.conn.execute('UPDATE trades SET closed=1, profit_loss=? WHERE id=?', (float(pnl), int(trade_id)))

    def close_position(self, symbol, close_price):
        pos = self.positions.get(symbol)
        if not pos:
            print("No position to close.")
            return

        pnl = (close_price - pos['price']) * pos['amount'] if pos['side'] == 'buy' else (pos['price'] - close_price) * pos['amount']
        # place market sell for long
        if pos['side'] == 'buy':
            self.place_order(symbol, 'sell', pos['amount'], close_price)
        else:
            self.place_order(symbol, 'buy', pos['amount'], close_price)

        # update DB entry (we stored trade_id in pos)
        trade_id = pos.get('trade_id')
        if trade_id:
            self.close_trade_db(trade_id, pnl)

        msg = f"💰 {symbol}: Trade closed | P/L {pnl:.4f} USDT"
        print(msg)
        # telegram + discord alert on close
        self.send_alerts(f"🔴 {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} | {msg}")
        self.positions[symbol] = None

    def breakout_confirmed(self, df):
        """
        Return True if breakout confirmation criteria are met:
         - last N closes above resistance*(1 + resistance_buffer_pct)
         - last bar volume > avg_volume * multiplier OR average of last N volumes > ...
        """
        c = self.cfg
        n = c['breakout_close_bars']
        if len(df) < max(n, c['volume_avg_window']) + 1:
            return False

        # use last n bars (most recent at -1)
        recent = df.iloc[-n:]
        # compute dynamic resistance values for these bars (we have per-row Resistance)
        # For breakout we compare closes to their corresponding resistance (use last resistance)
        last_resistance = df['Resistance'].iloc[-1]
        threshold = last_resistance * (1 + c['resistance_buffer_pct'])

        # 1) consecutive closes above threshold
        closes_ok = (recent['close'] > threshold).all()

        # 2) volume check: last bar volume > avg_volume * multiplier
        last_vol = df['volume'].iloc[-1]
        avg_vol = df['vol_avg'].iloc[-1] if not np.isnan(df['vol_avg'].iloc[-1]) else df['volume'].rolling(window=c['volume_avg_window'], min_periods=1).mean().iloc[-1]
        vol_ok = last_vol > (avg_vol * c['breakout_volume_multiplier'])

        return closes_ok and vol_ok

    def trade_logic(self, symbol, df):
        c = self.cfg
        latest = df.iloc[-1]
        balance = self.get_balance()
        current_price = float(latest['close'])
        trade_amount_usdt = max(balance['USDT'] * c['trade_allocation'], c['min_trade_usdt'])
        trade_amount = trade_amount_usdt / current_price
        pos = self.positions.get(symbol)

        # helper for pretty printing values that might be NaN
        def fmt(x, prec=4):
            try:
                return f"{float(x):.{prec}f}"
            except Exception:
                return "n/a"

        # --- Live Indicator Snapshot ---
        print(f"\n[{symbol}] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Price: {current_price:.4f} | EMA Fast: {fmt(latest.get('EMA_fast'))} | EMA Slow: {fmt(latest.get('EMA_slow'))}")
        print(f"MACD: {fmt(latest.get('MACD'),6)} | Signal: {fmt(latest.get('Signal'),6)} | RSI: {fmt(latest.get('RSI'),2)}")
        print(f"Support: {fmt(latest.get('Support'))} | Resistance: {fmt(latest.get('Resistance'))} | Vol Avg: {fmt(latest.get('vol_avg'))}")

        if pos:
            print(f"→ Open {pos['side'].upper()} @ {pos['price']:.4f} | SL: {pos['stop_loss']:.4f} | TP: {pos['take_profit']:.4f}")
        else:
            print("→ No open position")

        # --- Manage Existing Trades ---
        if pos:
            # stop loss or take profit check
            if current_price <= pos['stop_loss']:
                print(f"⚠️  {symbol}: STOP-LOSS triggered at {current_price:.4f}")
                self.close_position(symbol, current_price)
                return
            elif current_price >= pos['take_profit']:
                print(f"🎯  {symbol}: TAKE-PROFIT hit at {current_price:.4f}")
                self.close_position(symbol, current_price)
                return

        # --- Entry Logic ---
        if not pos:
            # Build list of enabled checks (booleans)
            checks = []

            # EMA: either cross event or simple sign check based on config
            if c.get('enable_ema', True):
                if c.get('use_ema_cross', True):
                    checks.append(self.is_bullish_ema_cross(df))
                else:
                    checks.append(latest['EMA_fast'] > latest['EMA_slow'])

            # MACD: either cross event or simple sign check based on config
            if c.get('enable_macd', True):
                if c.get('use_macd_cross', True):
                    checks.append(self.is_bullish_macd_cross(df))
                else:
                    checks.append(latest['MACD'] > latest['Signal'])

            if c.get('enable_rsi', True):
                checks.append(latest['RSI'] < c['rsi_buy_threshold'])

            if c.get('enable_support', True) and not np.isnan(latest.get('Support', np.nan)):
                checks.append(current_price > latest['Support'] * (1 + c['support_buffer_pct']))

            if c.get('enable_resistance', True) and not np.isnan(latest.get('Resistance', np.nan)):
                checks.append(current_price < latest['Resistance'] * (1 - c['resistance_buffer_pct']))

            # require at least min_checks_to_buy of the enabled checks to be true
            enabled_checks_count = len(checks)
            true_checks = sum(1 for v in checks if bool(v))
            min_req = int(c.get('min_checks_to_buy', 2))
            buy_condition_base = (enabled_checks_count > 0) and (true_checks >= min_req)

            # Breakout entry: price closes above resistance + confirmation (only if breakout enabled)
            breakout_condition = False
            if c.get('enable_breakout', False) and not np.isnan(latest.get('Resistance', np.nan)):
                if current_price > latest['Resistance'] * (1 + c['resistance_buffer_pct']):
                    breakout_condition = self.breakout_confirmed(df)

            # Final decision: either safe reversal buy OR breakout buy
            if buy_condition_base or breakout_condition:
                stop_loss = current_price * (1 - c['stop_loss_pct'])
                take_profit = current_price * (1 + c['take_profit_pct'])
                order = self.place_order(symbol, 'buy', trade_amount, current_price)

                # log trade and keep track of trade id in memory
                trade_id = self.log_trade(symbol, order, stop_loss, take_profit)
                self.positions[symbol] = {
                    'side': 'buy',
                    'price': current_price,
                    'amount': trade_amount,
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'trade_id': trade_id
                }

                reason = "BREAKOUT" if breakout_condition else "CHANNEL_ENTRY"
                print(f"✅ {symbol}: BUY @ {current_price:.4f} | SL {stop_loss:.4f} | TP {take_profit:.4f} | REASON: {reason}")

                # send detailed alert (includes SL/TP) after position is recorded
                timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                alert_msg = (f"🟢 {timestamp} | {symbol} | BUY {trade_amount:.8f} @ {current_price:.8f} "
                             f"| SL {stop_loss:.8f} | TP {take_profit:.8f} | REASON: {reason}")
                self.send_alerts(alert_msg)

    def summarize_performance(self):
        df = pd.read_sql('SELECT * FROM trades', self.conn)
        total_profit = df['profit_loss'].sum()
        print(f"\n📊 Total simulated profit: {total_profit:.4f} USDT | Trades: {len(df)}")
        return total_profit

    def on_exit(self):
        """Attempt to notify Discord/Telegram that the bot is stopping."""
        try:
            msg = f"🛑 Bot stopped: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}"
            self.send_alerts(msg)
            # small delay to allow network I/O to complete when possible
            time.sleep(0.5)
        except Exception:
            # suppress any errors in exit handler
            pass

    def run(self):
        print(f"🚀 Running Binance Spot Multi-Pair Bot ({', '.join(self.cfg['symbols'])})\n")
        try:
            while True:
                try:
                    for symbol in self.cfg['symbols']:
                        df = self.fetch_data(symbol)
                        df = self.calculate_indicators(df)
                        self.trade_logic(symbol, df)
                    self.summarize_performance()
                    time.sleep(self.cfg['poll_interval'])
                except KeyboardInterrupt:
                    # user requested stop
                    print("Interrupted by user (KeyboardInterrupt). Exiting loop.")
                    self.send_alerts("⛔ Bot interrupted by user (KeyboardInterrupt). Exiting.")
                    break
                except Exception as e:
                    tb = traceback.format_exc()
                    print(f"❌ Error: {e}\n{tb}")
                    # Limit traceback size to avoid too-large messages
                    tb_short = tb if len(tb) <= 1500 else tb[:1500] + "\n...[truncated]"
                    self.send_alerts(f"❌ Error in bot: {e}\nTraceback:\n{tb_short}")
                    time.sleep(5)
        except Exception as e:
            # catch any outer/fatal exception
            tb = traceback.format_exc()
            print(f"💥 Fatal error: {e}\n{tb}")
            tb_short = tb if len(tb) <= 1500 else tb[:1500] + "\n...[truncated]"
            try:
                self.send_alerts(f"💥 Fatal error: {e}\nTraceback:\n{tb_short}")
            except Exception:
                pass
            # re-raise so process exit code reflects failure if desired
            raise
        finally:
            # ensure a final shutdown alert (also handled by atexit)
            try:
                self.send_alerts(f"🟥 Bot exiting: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
                time.sleep(0.5)
            except Exception:
                pass

    # --- new helper methods for detecting cross events ---
    def is_bullish_ema_cross(self, df):
        """Return True if EMA_fast crossed above EMA_slow between previous and last bar."""
        if len(df) < 2:
            return False
        prev = df.iloc[-2]
        cur = df.iloc[-1]
        try:
            ef_prev = float(prev['EMA_fast'])
            es_prev = float(prev['EMA_slow'])
            ef_cur = float(cur['EMA_fast'])
            es_cur = float(cur['EMA_slow'])
        except Exception:
            return False
        return (ef_prev <= es_prev) and (ef_cur > es_cur)

    def is_bullish_macd_cross(self, df):
        """Return True if MACD crossed above Signal between previous and last bar."""
        if len(df) < 2:
            return False
        prev = df.iloc[-2]
        cur = df.iloc[-1]
        try:
            m_prev = float(prev['MACD'])
            s_prev = float(prev['Signal'])
            m_cur = float(cur['MACD'])
            s_cur = float(cur['Signal'])
        except Exception:
            return False
        return (m_prev <= s_prev) and (m_cur > s_cur)
    # --- end new helpers ---

if __name__ == '__main__':
    bot = MultiPairBot(CONFIG)
    bot.run()
