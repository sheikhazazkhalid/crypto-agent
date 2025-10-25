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
    'symbols': ['BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'XRP/USDT', 'SOL/USDT', 'ADA/USDT', 'LINK/USDT', 'AVAX/USDT', 'POL/USDT', 'LTC/USDT', 'DOT/USDT', 'ATOM/USDT'],
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

    # Indicators / flow
    'enable_ema': True,
    'enable_macd': True,
    'enable_rsi': True,
    'enable_volume_avg': True,
    # New: entry volume confirmation (optional)
    'enable_volume_confirmation': False,  # set True to require volume confirmation on entries
    'volume_confirm_multiplier': 1.2,     # require volume >= multiplier * vol_avg

    # Risk Management
    'trade_allocation': 0.10,         # % of available USDT balance per trade
    'min_trade_usdt': 10.0,           # Minimum USDT per trade
    'stop_loss_pct': 0.01,            # 1% stop loss
    'take_profit_pct': 0.015,         # 1.5% take profit

    # Indicator params
    'ema_fast_span': 12,
    'ema_slow_span': 26,
    'macd_signal_span': 9,
    'rsi_period': 14,
    'rsi_buy_threshold': 30,          # RSI drop threshold (watch)
    'ema_200_span': 200,              # 200 EMA filter
    'cross_lookback': 8,              # how many past bars to scan for MACD cross after RSI drop

    # volume
    'volume_avg_window': 20,

    # max concurrent open positions (<=0 means unlimited)
    'max_open_positions': 0,

    # order type
    'order_type': 'market',  # 'market' or 'limit'
    'limit_price_buffer_pct': 0.001,  # 0.1% buffer for limit orders (above for buy, below for sell)
    'exchange_for_data': 'mexc',  
    'exchange_for_trading': 'binance', # 'binance' for global trading (your account), 'binanceus' for US
}



class MultiPairBot:
    def __init__(self, cfg):
        self.cfg = cfg
        # Use separate clients for data and trading
        self.data_client = ccxt.__dict__[cfg['exchange_for_data']]({
            'apiKey': '',  # no keys needed for data
            'secret': '',
            'options': {'defaultType': 'spot'}
        })
        self.client = ccxt.__dict__[cfg['exchange_for_trading']]({
            'apiKey': cfg['api_key'],
            'secret': cfg['api_secret'],
            'options': {'defaultType': 'spot'}
        })
        #self.client.set_sandbox_mode(True)  # optional for testing
        self.conn = sqlite3.connect('trades.db', check_same_thread=False)
        self.create_tables()
        self.positions = {symbol: None for symbol in cfg['symbols']}
        # explicitly track RSI-drop watchers per symbol
        self.rsi_drops = {symbol: None for symbol in cfg['symbols']}
        # register exit handler to notify on normal exit
        try:
            atexit.register(self.on_exit)
        except Exception:
            pass


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
        # Use data_client for OHLCV
        ohlcv = self.data_client.fetch_ohlcv(symbol, self.cfg['timeframe'], limit=self.cfg['limit'])
        df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','volume'])
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        # set time as index so row.name / .index timestamps are reliable for comparisons
        df.set_index('time', inplace=True)
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

        def safe_ewm(series, span):
            return series.ewm(span=span, adjust=False).mean()

        # EMAs (include 200 EMA used as trend filter)
        if c.get('enable_ema', True):
            df['EMA_fast'] = safe_ewm(df['close'], c['ema_fast_span'])
            df['EMA_slow'] = safe_ewm(df['close'], c['ema_slow_span'])
            df['EMA_200'] = safe_ewm(df['close'], c.get('ema_200_span', 200))
        else:
            df['EMA_fast'] = np.nan
            df['EMA_slow'] = np.nan
            df['EMA_200'] = np.nan

        # MACD (simple MACD using EMA_fast - EMA_slow)
        if c.get('enable_macd', True) and c.get('enable_ema', True):
            df['MACD'] = df['EMA_fast'] - df['EMA_slow']
            df['Signal'] = df['MACD'].ewm(span=c['macd_signal_span'], adjust=False).mean()
        else:
            df['MACD'] = np.nan
            df['Signal'] = np.nan

        # RSI (Wilder)
        if c.get('enable_rsi', True):
            df['RSI'] = self.compute_rsi(df['close'], c['rsi_period'])
        else:
            df['RSI'] = np.nan

        # volume average (kept)
        if c.get('enable_volume_avg', True):
            df['vol_avg'] = df['volume'].rolling(window=c['volume_avg_window']).mean()
        else:
            df['vol_avg'] = np.nan

        # remove support/resistance from previous logic (not used)
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

    def place_order(self, symbol, side, amount, price, notify=True):
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        order_type = self.cfg.get('order_type', 'market')
        if self.cfg['dry_run']:
            msg = f"[DRY RUN] {timestamp} | {symbol} | {side.upper()} {amount:.8f} @ {price:.8f} ({order_type})"
            print(msg)
            if notify:
                self.send_alerts(f"🟡 {msg}")
            return {'symbol': symbol, 'side': side, 'price': price, 'amount': amount}
        else:
            if order_type == 'limit':
                # calculate limit price with buffer
                buffer = self.cfg.get('limit_price_buffer_pct', 0.001)
                if side.lower() == 'buy':
                    limit_price = price * (1 + buffer)  # buy slightly above
                else:
                    limit_price = price * (1 - buffer)  # sell slightly below
                order = self.client.create_limit_order(symbol, side, amount, limit_price)
            else:
                # market order
                if side.lower() == 'buy':
                    order = self.client.create_market_buy_order(symbol, amount)
                else:
                    order = self.client.create_market_sell_order(symbol, amount)
            # extract executed details
            executed_price = float(order.get('price', price)) if isinstance(order, dict) else price
            executed_amount = float(order.get('amount', amount)) if isinstance(order, dict) else amount
            msg = f"[EXECUTED] {timestamp} | {symbol} | {side.upper()} {executed_amount:.8f} @ {executed_price:.8f} ({order_type})"
            print(msg)
            if notify:
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
            self.place_order(symbol, 'sell', pos['amount'], close_price, notify=False)
        else:
            self.place_order(symbol, 'buy', pos['amount'], close_price, notify=False)

        # update DB entry (we stored trade_id in pos)
        trade_id = pos.get('trade_id')
        if trade_id:
            self.close_trade_db(trade_id, pnl)

        msg = f"💰 {symbol}: Trade closed | P/L {pnl:.4f} USDT"
        print(msg)
        # telegram + discord alert on close
        self.send_alerts(f"🔴 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} | {msg}")
        self.positions[symbol] = None


    def trade_logic(self, symbol, df):
        """
        New simplified flow:
         1) If RSI drops below rsi_buy_threshold -> mark symbol as watching (rsi_drops)
         2) While watching, wait for MACD bullish cross that occurs AFTER the RSI drop
         3) When cross confirmed and latest price > EMA_200 -> BUY with SL/TP (as configured)
         4) Optional: require volume confirmation (current volume >= multiplier * vol_avg)
        """
        c = self.cfg
        latest = df.iloc[-1]
        balance = self.get_balance()
        current_price = float(latest['close'])
        trade_amount_usdt = max(balance['USDT'] * c['trade_allocation'], c['min_trade_usdt'])
        trade_amount = trade_amount_usdt / current_price
        pos = self.positions.get(symbol)

        def fmt(x, prec=4):
            try:
                return f"{float(x):.{prec}f}"
            except Exception:
                return "n/a"

        print(f"\n[{symbol}] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(
            f"Price: {current_price:.4f} | RSI: {fmt(latest.get('RSI'))} | MACD: {fmt(latest.get('MACD'),6)} | "
            f"Signal: {fmt(latest.get('Signal'),6)} | EMA200: {fmt(latest.get('EMA_200'))} | "
            f"Vol: {fmt(latest.get('volume'))} | VAvg: {fmt(latest.get('vol_avg'))}"
        )

        # Manage existing trade (SL/TP)
        if pos:
            if current_price <= pos['stop_loss']:
                print(f"⚠️  {symbol}: STOP-LOSS triggered at {current_price:.4f}")
                self.close_position(symbol, current_price)
                return
            elif current_price >= pos['take_profit']:
                print(f"🎯  {symbol}: TAKE-PROFIT hit at {current_price:.4f}")
                self.close_position(symbol, current_price)
                return

        # New entry flow only
        if not pos:
            # 1) Record RSI drop -> start watcher
            rsi_val = None
            try:
                rsi_val = float(latest['RSI'])
            except Exception:
                pass

            if rsi_val is not None:
                # require an actual CROSS down into the buy zone: previous RSI must be >= threshold
                buy_th = float(c.get('rsi_buy_threshold', 30))
                prev_rsi = None
                if len(df) >= 2:
                    try:
                        prev_rsi = float(df['RSI'].iloc[-2])
                    except Exception:
                        prev_rsi = None

                if rsi_val < buy_th and (prev_rsi is None or prev_rsi >= buy_th):
                    if getattr(self, 'rsi_drops', None) is None:
                        self.rsi_drops = {s: None for s in c['symbols']}
                    if self.rsi_drops.get(symbol) is None:
                        # prefer indexed candle timestamp if available
                        try:
                            watch_time = df.index[-1]
                        except Exception:
                            watch_time = latest.get('time') or pd.Timestamp.now()
                        self.rsi_drops[symbol] = watch_time
                        print(f"🔎 {symbol}: RSI crossed below {buy_th} ({rsi_val:.2f}) — watching for MACD bullish cross")
                # NOTE: do NOT clear watcher on RSI recovery here — keep watching until a MACD cross is processed
            # 2) If watching, search for MACD bullish cross after recorded RSI-drop time
            watch_time = getattr(self, 'rsi_drops', {}).get(symbol)
            if watch_time is not None:
                macd_cross_time = None
                if c.get('enable_macd', True):
                    macd_cross_time = self.find_bullish_macd_cross_time(df, lookback=c.get('cross_lookback', 8), after_time=watch_time)
                if macd_cross_time is not None:
                    # advance watcher to the cross time so we don't repeatedly process the same cross
                    self.rsi_drops[symbol] = macd_cross_time

                    # Additional check: ensure MACD is still bullish at the latest bar
                    macd_bullish_now = latest.get('MACD', np.nan) > latest.get('Signal', np.nan)
                    if not macd_bullish_now:
                        print(f"⛔ {symbol}: MACD cross found at {macd_cross_time} but MACD not bullish at latest bar — skipping buy for this cross (will watch for next cross)")
                        return  # keep watcher active (advanced), wait for next cross

                    # 3) Confirm trend: price above 200 EMA
                    ema200 = latest.get('EMA_200', np.nan)
                    if not np.isnan(ema200) and current_price > float(ema200):
                        # Optional volume confirmation
                        if c.get('enable_volume_confirmation', False):
                            try:
                                cur_vol = float(latest.get('volume', np.nan))
                            except Exception:
                                cur_vol = np.nan
                            vavg = latest.get('vol_avg', np.nan)
                            if np.isnan(vavg):
                                # fallback compute if vol_avg disabled or not ready
                                try:
                                    vavg = float(df['volume'].rolling(window=c.get('volume_avg_window', 20)).mean().iloc[-1])
                                except Exception:
                                    vavg = np.nan
                            multiplier = float(c.get('volume_confirm_multiplier', 1.0))
                            vol_ok = (not np.isnan(cur_vol)) and (not np.isnan(vavg)) and (cur_vol >= multiplier * vavg)
                            if not vol_ok:
                                print(f"⛔ {symbol}: Volume confirmation failed at cross — vol {fmt(cur_vol)} < {multiplier}× vavg {fmt(vavg)} (skipping buy for this cross)")
                                # Clear watcher on volume confirmation failure
                                self.rsi_drops[symbol] = None

                        # enforce max open positions limit
                        if not self.can_open_new_trade():
                            print(f"⛔ {symbol}: max open positions reached ({self.get_open_positions_count()}/{c.get('max_open_positions')}) - skipping buy")
                            return  # keep watcher active (already advanced)

                        stop_loss = current_price * (1 - c['stop_loss_pct'])
                        take_profit = current_price * (1 + c['take_profit_pct'])
                        order = self.place_order(symbol, 'buy', trade_amount, current_price, notify=False)
                        trade_id = self.log_trade(symbol, order, stop_loss, take_profit)
                        self.positions[symbol] = {
                            'side': 'buy',
                            'price': current_price,
                            'amount': trade_amount,
                            'stop_loss': stop_loss,
                            'take_profit': take_profit,
                            'trade_id': trade_id
                        }

                        reason = "RSI_DROP+MACD_CROSS+EMA200"
                        print(f"✅ {symbol}: BUY @ {current_price:.4f} | SL {stop_loss:.4f} | TP {take_profit:.4f} | REASON: {reason}")

                        # send detailed alert (includes SL/TP) after position is recorded
                        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                        alert_msg = (f"🟢 {timestamp} | {symbol} | BUY {trade_amount:.8f} @ {current_price:.8f} "
                                     f"| SL {stop_loss:.8f} | TP {take_profit:.8f} | REASON: {reason}")
                        self.send_alerts(alert_msg)

                        # clear watcher on successful buy
                        self.rsi_drops[symbol] = None
                    else:
                       print(f"⛔ {symbol}: MACD crossed at {macd_cross_time} after RSI drop but EMA200 filter failed ({current_price:.4f} <= {ema200 if not np.isnan(ema200) else 'n/a'}) — clearing watcher for this RSI-drop")
                       self.rsi_drops[symbol] = None
                # else: keep waiting

    def summarize_performance(self):
        df = pd.read_sql('SELECT * FROM trades', self.conn)
        total_profit = df['profit_loss'].sum()
        print(f"\n📊 Total simulated profit: {total_profit:.4f} USDT | Trades: {len(df)}")
        return total_profit

    def on_exit(self):
        """Attempt to notify Discord/Telegram that the bot is stopping."""
        try:
            msg = f"🛑 Bot stopped: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
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
                self.send_alerts(f"🟥 Bot exiting: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
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

    def find_bullish_macd_cross_time(self, df, lookback=None, after_time=None):
        """Scan the last `lookback` closed candles for the FIRST MACD crossing above Signal AFTER `after_time`.
        Return timestamp of the first qualifying cross candle (pd.Timestamp) or None.
        """
        if lookback is None:
            lookback = int(self.cfg.get('cross_lookback', 8))
        if len(df) < 2:
            return None
        start = max(1, len(df) - lookback)
        for i in range(start, len(df)):
            try:
                prev = df.iloc[i - 1]
                cur = df.iloc[i]
                cross_happened = (float(prev['MACD']) <= float(prev['Signal'])) and (float(cur['MACD']) > float(cur['Signal']))
                if cross_happened:
                    cross_time = cur.get('time') or cur.name
                    if after_time is None or pd.to_datetime(cross_time) > pd.to_datetime(after_time):
                        return cross_time  # return the FIRST one after after_time
            except Exception:
                continue
        return None

    def get_open_positions_count(self):
        """Return number of currently open positions tracked in memory."""
        return sum(1 for p in self.positions.values() if p)

    def can_open_new_trade(self):
        """Check configured limit; return True if we may open another trade."""
        maxp = int(self.cfg.get('max_open_positions', 0) or 0)
        if maxp <= 0:
            return True  # unlimited
        return self.get_open_positions_count() < maxp

if __name__ == '__main__':
    bot = MultiPairBot(CONFIG)
    bot.run()
