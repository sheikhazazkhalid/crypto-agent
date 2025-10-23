import ccxt
import pandas as pd
import numpy as np
import sqlite3
import time
from datetime import datetime

CONFIG = {
    # ==== Exchange / General ====
    'api_key': '',                    # Binance/Bybit API key (optional for dry-run)
    'api_secret': '',                 # Binance/Bybit API secret (optional for dry-run)
    'symbols': ['BTC/USDT', 'ETH/USDT'],  # Trading pairs to monitor
    'timeframe': '5m',                # Candle interval: 5m gives smoother signals than 1m
    'limit': 300,                     # Number of candles to fetch (for indicators)
    'poll_interval': 30,              # Seconds between each data fetch / cycle
    'dry_run': True,                  # True = no real trades, for safe testing

    # ==== Risk Management ====
    'trade_allocation': 0.05,         # % of available USDT balance per trade (e.g., 5%)
    'min_trade_usdt': 10.0,           # Minimum USDT per trade (to skip dust orders)
    'stop_loss_pct': 0.025,           # 2.5% stop loss from entry price
    'take_profit_pct': 0.04,          # 4% take profit target → 1:1.6 R:R ratio approx.

    # ==== Indicator Config ====
    'ema_fast_span': 12,              # Short-term EMA for trend detection
    'ema_slow_span': 26,              # Long-term EMA for trend confirmation
    'macd_signal_span': 9,            # MACD signal line smoothing period
    'rsi_period': 14,                 # RSI lookback window
    'rsi_buy_threshold': 40,          # Buy if RSI < 40 (mildly oversold)
    'rsi_sell_threshold': 60,         # Exit if RSI > 60 (momentum slowing)

    # ==== Support / Resistance ====
    'sr_mode': 'pivot',               # Options: 'pivot', 'rolling', 'fractal'
    'support_window': 20,             # Lookback window for rolling or fractal SR calc
    'support_buffer_pct': 0.003,      # +0.3% above support → safe entry zone
    'resistance_buffer_pct': 0.003,   # -0.3% below resistance → avoid buying too high

    # ==== Breakout Confirmation (Optional) ====
    'enable_breakout': True,          # Enable breakout validation logic
    'breakout_close_bars': 2,         # Require 2 consecutive closes above resistance
    'breakout_volume_multiplier': 1.5,# Volume must exceed avg_volume * multiplier
    'volume_avg_window': 20,          # Lookback for average volume calculation
}



class MultiPairBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = ccxt.binance({
            'apiKey': cfg['api_key'],
            'secret': cfg['api_secret'],
            'options': {'defaultType': 'spot'}
        })
        self.client.set_sandbox_mode(True)
        self.conn = sqlite3.connect('trades.db', check_same_thread=False)
        self.create_tables()
        self.positions = {symbol: None for symbol in cfg['symbols']}

    def create_tables(self):
        with self.conn:
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

    def fetch_data(self, symbol):
        ohlcv = self.client.fetch_ohlcv(symbol, self.cfg['timeframe'], limit=self.cfg['limit'])
        df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','volume'])
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        df.set_index('time', inplace=False)
        return df

    def compute_rsi(self, prices, period):
        delta = prices.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)

        # Use simple moving average for first 'period' then Wilder smoothing (optional)
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()

        # After initial values, apply Wilder smoothing
        avg_gain = avg_gain.ffill()
        avg_loss = avg_loss.ffill()


        rs = avg_gain / (avg_loss.replace(0, np.nan))
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.fillna(50)  # neutral for the first rows
        return rsi

    def calculate_indicators(self, df):
        c = self.cfg
        df = df.copy()
        df['EMA_fast'] = df['close'].ewm(span=c['ema_fast_span'], adjust=False).mean()
        df['EMA_slow'] = df['close'].ewm(span=c['ema_slow_span'], adjust=False).mean()
        df['MACD'] = df['EMA_fast'] - df['EMA_slow']
        df['Signal'] = df['MACD'].ewm(span=c['macd_signal_span'], adjust=False).mean()
        df['RSI'] = self.compute_rsi(df['close'], c['rsi_period'])
        df['vol_avg'] = df['volume'].rolling(window=c['volume_avg_window']).mean()

        mode = c.get('sr_mode', 'rolling').lower()

        if mode == 'pivot':
            # Classic pivot points (per bar) - then we will use most recent pivot levels
            df['Pivot'] = (df['high'] + df['low'] + df['close']) / 3
            df['Support'] = (2 * df['Pivot']) - df['high']
            df['Resistance'] = (2 * df['Pivot']) - df['low']

            # For stability, take rolling mean of last few pivot-derived levels
            df['Support'] = df['Support'].rolling(window=3, min_periods=1).mean()
            df['Resistance'] = df['Resistance'].rolling(window=3, min_periods=1).mean()

        elif mode == 'fractal':
            # Fractal detection using center window (5 candles)
            def support_fractal(arr):
                # arr: [low(-2), low(-1), low(0), low(+1), low(+2)]
                mid = arr[2]
                if mid == np.nanmin(arr):
                    return mid
                return np.nan

            def resistance_fractal(arr):
                mid = arr[2]
                if mid == np.nanmax(arr):
                    return mid
                return np.nan

            df['Support'] = df['low'].rolling(window=5, center=True).apply(
                lambda x: x[2] if x[2] == x.min() else np.nan, raw=True)
            df['Resistance'] = df['high'].rolling(window=5, center=True).apply(
                lambda x: x[2] if x[2] == x.max() else np.nan, raw=True)

            # Forward/backfill to give usable levels until new fractal appears
            df['Support'] = df['Support'].ffill().bfill()
            df['Resistance'] = df['Resistance'].ffill().bfill()

        else:
            # rolling min/max (simple)
            df['Support'] = df['low'].rolling(window=c['support_window'], min_periods=1).min()
            df['Resistance'] = df['high'].rolling(window=c['support_window'], min_periods=1).max()

        # Avoid NaNs for the last row by filling from previous values if necessary
        df['Support'] = df['Support'].ffill().bfill()
        df['Resistance'] = df['Resistance'].ffill().bfill()


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
        if self.cfg['dry_run']:
            print(f"[DRY RUN] {symbol}: {side.upper()} {amount:.8f} @ {price:.8f}")
            return {'symbol': symbol, 'side': side, 'price': price, 'amount': amount}
        else:
            # market order (price not used by API, kept for logging)
            if side.lower() == 'buy':
                order = self.client.create_market_buy_order(symbol, amount)
            else:
                order = self.client.create_market_sell_order(symbol, amount)
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
            # update by id for correctness
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

        print(f"💰 {symbol}: Trade closed | P/L {pnl:.4f} USDT")
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

        # --- Live Indicator Snapshot ---
        print(f"\n[{symbol}] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Price: {current_price:.4f} | EMA Fast: {latest['EMA_fast']:.4f} | EMA Slow: {latest['EMA_slow']:.4f}")
        print(f"MACD: {latest['MACD']:.6f} | Signal: {latest['Signal']:.6f} | RSI: {latest['RSI']:.2f}")
        print(f"Support: {latest['Support']:.4f} | Resistance: {latest['Resistance']:.4f} | Vol Avg: {latest['vol_avg']:.4f}")

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
            # Basic reversal entry inside support/resistance channel
            buy_condition_base = (
                (latest['EMA_fast'] > latest['EMA_slow']) and
                (latest['MACD'] > latest['Signal']) and
                (latest['RSI'] < c['rsi_buy_threshold']) and
                (current_price > latest['Support'] * (1 + c['support_buffer_pct'])) and
                (current_price < latest['Resistance'] * (1 - c['resistance_buffer_pct']))
            )

            # Breakout entry: price closes above resistance + confirmation
            breakout_condition = False
            if c.get('enable_breakout', False):
                # price must be above resistance buffer and breakout_confirmed
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

    def summarize_performance(self):
        df = pd.read_sql('SELECT * FROM trades', self.conn)
        total_profit = df['profit_loss'].sum()
        print(f"\n📊 Total simulated profit: {total_profit:.4f} USDT | Trades: {len(df)}")
        return total_profit

    def run(self):
        print(f"🚀 Running Binance Spot Multi-Pair Bot ({', '.join(self.cfg['symbols'])})\n")
        while True:
            try:
                for symbol in self.cfg['symbols']:
                    df = self.fetch_data(symbol)
                    df = self.calculate_indicators(df)
                    self.trade_logic(symbol, df)
                self.summarize_performance()
                time.sleep(self.cfg['poll_interval'])
            except Exception as e:
                print(f"❌ Error: {e}")
                time.sleep(5)

if __name__ == '__main__':
    bot = MultiPairBot(CONFIG)
    bot.run()
