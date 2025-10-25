import sys
from time import sleep
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

def compute_local_indicators(df, cfg):
    df = df.copy().reset_index(drop=True)
    # EMAs
    ema_f = ewma(df['close'], cfg['ema_fast_span'])
    ema_s = ewma(df['close'], cfg['ema_slow_span'])
    ema_200 = ewma(df['close'], cfg.get('ema_200_span', 200))
    # MACD / Signal
    macd = ema_f - ema_s
    signal = macd.ewm(span=cfg['macd_signal_span'], adjust=False).mean()
    # RSI
    rsi = wilder_rsi(df['close'], cfg['rsi_period'])
    return pd.DataFrame({
        'time': df['time'],
        'close': df['close'],
        'EMA_fast': ema_f,
        'EMA_slow': ema_s,
        'EMA_200': ema_200,
        'MACD': macd,
        'Signal': signal,
        'RSI': rsi
    })

def compare_symbol(bot, cfg, symbol, rows=10):
    print(f"\n=== VERIFY: {symbol} ===")
    # keep the 'time' column when resetting index so comparisons use timestamps
    df = bot.fetch_data(symbol).reset_index()
    if df.empty:
        print(f"No data available for {symbol} — skipping.")
        return
    df_bot = bot.calculate_indicators(df).reset_index()
    df_local = compute_local_indicators(df, cfg)

    n = min(rows, len(df))
    start = len(df) - n
    for i in range(start, len(df)):
        t = df.at[i, 'time']
        close = df.at[i, 'close']
        print(f"\nROW {i} time={t} close={close}")
        for col in ('EMA_fast','EMA_slow','EMA_200','MACD','Signal','RSI'):
            a = df_bot.at[i, col] if col in df_bot.columns else np.nan
            b = df_local.at[i, col]
            try:
                diff = float(a) - float(b)
                print(f"  {col}: bot={a:.8f} local={b:.8f} diff={diff:.8f}")
            except Exception:
                print(f"  {col}: bot={a} local={b}")

    # summary last row diffs
    last = -1
    print("\n--- DIFF SUMMARY (last row) ---")
    def diff(a,b,name):
        try:
            a=float(a); b=float(b); d=a-b
            print(f"{name} diff: {d:.8f}")
        except Exception:
            print(f"{name}: n/a")
    if len(df_bot) > 0 and len(df_local) > 0:
        # use positional indexing (.iloc) to get the last row reliably
        try:
            diff(df_bot.iloc[last]['EMA_fast'],  df_local.iloc[last]['EMA_fast'],  'EMA_fast')
            diff(df_bot.iloc[last]['EMA_slow'],  df_local.iloc[last]['EMA_slow'],  'EMA_slow')
            diff(df_bot.iloc[last]['EMA_200'],   df_local.iloc[last]['EMA_200'],   'EMA_200')
            diff(df_bot.iloc[last]['MACD'],      df_local.iloc[last]['MACD'],      'MACD')
            diff(df_bot.iloc[last]['Signal'],    df_local.iloc[last]['Signal'],    'Signal')
            diff(df_bot.iloc[last]['RSI'],       df_local.iloc[last]['RSI'],       'RSI (wilder)')
        except Exception as e:
            print(f"diff summary failed: {e}")
    else:
        print("No data for diff summary.")

def main():
    bot = MultiPairBot(CONFIG)
    # symbol argument optional; if not provided iterate all symbols in CONFIG
    if len(sys.argv) > 1:
        symbols = [sys.argv[1]]
    else:
        symbols = CONFIG.get('symbols', [])
    for sym in symbols:
        try:
            compare_symbol(bot, CONFIG, sym, rows=10)
            sleep(1)  # to avoid rate limits
        except Exception as e:
            print(f"[error] {sym}: {e}")

if __name__ == '__main__':
    main()