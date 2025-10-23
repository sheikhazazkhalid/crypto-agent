# ...existing code...
import sys
import pandas as pd
import numpy as np

from main import MultiPairBot, CONFIG

def ewma(series, span):
    return series.ewm(span=span, adjust=False).mean()

def compute_macd(series, fast_span, slow_span, signal_span):
    ema_fast = ewma(series, fast_span)
    ema_slow = ewma(series, slow_span)
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=signal_span, adjust=False).mean()
    return ema_fast, ema_slow, macd, signal

def wilder_rsi(series, period):
    delta = series.diff()
    up = delta.where(delta > 0, 0.0)
    down = -delta.where(delta < 0, 0.0)
    avg_up = up.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_down = down.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = avg_up / avg_down.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)

def compare(symbol='BTC/USDT'):
    bot = MultiPairBot(CONFIG)
    df = bot.fetch_data(symbol).reset_index(drop=True)
    df_bot = bot.calculate_indicators(df).reset_index(drop=True)

    cfg = CONFIG
    ema_f, ema_s, macd, signal = compute_macd(df['close'], cfg['ema_fast_span'], cfg['ema_slow_span'], cfg['macd_signal_span'])
    rsi_wilder = wilder_rsi(df['close'], cfg['rsi_period'])

    rows = 10
    print("\n--- LAST ROWS: bot vs local calc (no pandas_ta) ---\n")
    for i in range(-rows, 0):
        idx = len(df) + i
        time = df.at[idx, 'time']
        close = df.at[idx, 'close']
        print(f"ROW {idx} time={time} close={close}")
        print(f"  EMA_fast_bot={df_bot.at[idx,'EMA_fast']:.8f} / EMA_fast_local={ema_f.at[idx]:.8f}")
        print(f"  EMA_slow_bot={df_bot.at[idx,'EMA_slow']:.8f} / EMA_slow_local={ema_s.at[idx]:.8f}")
        print(f"  MACD_bot={df_bot.at[idx,'MACD']:.8f} / MACD_local={macd.at[idx]:.8f}")
        print(f"  Signal_bot={df_bot.at[idx,'Signal']:.8f} / Signal_local={signal.at[idx]:.8f}")
        print(f"  RSI_bot={df_bot.at[idx,'RSI']:.8f} / RSI_wilder={rsi_wilder.at[idx]:.8f}")
        print("")

    # diff summary last row
    def diff(a,b,name):
        try:
            a=float(a); b=float(b); print(f"{name} diff: {a-b:.8f}")
        except: print(f"{name}: n/a")
    last = -1
    print("\n--- DIFF SUMMARY (last row) ---")
    diff(df_bot['EMA_fast'].iloc[last], ema_f.iloc[last], 'EMA_fast')
    diff(df_bot['EMA_slow'].iloc[last], ema_s.iloc[last], 'EMA_slow')
    diff(df_bot['MACD'].iloc[last], macd.iloc[last], 'MACD')
    diff(df_bot['Signal'].iloc[last], signal.iloc[last], 'Signal')
    diff(df_bot['RSI'].iloc[last], rsi_wilder.iloc[last], 'RSI (wilder)')

if __name__ == '__main__':
    sym = sys.argv[1] if len(sys.argv)>1 else 'BTC/USDT'
    compare(sym)