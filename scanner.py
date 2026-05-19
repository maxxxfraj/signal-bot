import ccxt
import pandas as pd
import ta
from backtest import run_backtest

exchange = ccxt.binance()

WATCHLIST = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT',
    'XRP/USDT', 'DOGE/USDT', 'ADA/USDT', 'AVAX/USDT',
    'DOT/USDT', 'POL/USDT', 'LINK/USDT', 'UNI/USDT',
    'ATOM/USDT', 'LTC/USDT', 'ETC/USDT', 'FIL/USDT',
]

TIMEFRAMES = ['5m', '15m', '1h', '4h', '1d']

def get_candles(symbol, timeframe, limit=1000):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except Exception as e:
        print(f"Помилка отримання свічок {symbol}: {e}")
        return None

def calculate_indicators(df):
    df['ema20'] = ta.trend.ema_indicator(df['close'], window=20)
    df['ema50'] = ta.trend.ema_indicator(df['close'], window=50)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['atr'] = ta.volatility.average_true_range(
        df['high'], df['low'], df['close'], window=14
    )
    return df

def find_signal(symbol, timeframe):
    df = get_candles(symbol, timeframe, limit=1000)
    if df is None or len(df) < 60:
        return None

    df = calculate_indicators(df)
    df = df.dropna()

    if len(df) < 3:
        return None

    # Остання закрита свічка для індикаторів
    last = df.iloc[-2]
    # Поточна свічка для ціни входу
    current = df.iloc[-1]

    price = current['close']
    rsi = last['rsi']
    ema20 = last['ema20']
    ema50 = last['ema50']
    atr = last['atr']

    if pd.isna(rsi) or pd.isna(ema20) or pd.isna(ema50) or pd.isna(atr):
        return None

    direction = None

    if (last['close'] < ema20 and
        last['close'] < ema50 and
        45 < rsi < 60 and
        last['close'] < last['open']):
        direction = 'SHORT'

    elif (last['close'] > ema20 and
          last['close'] > ema50 and
          40 < rsi < 55 and
          last['close'] > last['open']):
        direction = 'LONG'

    if direction is None:
        return None

    entry = round(price, 6)

    if direction == 'SHORT':
        dobar_low = round(entry + atr * 0.5, 6)
        dobar_high = round(entry + atr * 1.5, 6)
        stop_loss = round(entry + atr * 2.0, 6)
        tps = [
            (round(entry - atr * 0.8, 6), 90, round(atr * 0.8 / entry * 100, 1)),
            (round(entry - atr * 1.3, 6), 75, round(atr * 1.3 / entry * 100, 1)),
            (round(entry - atr * 2.0, 6), 58, round(atr * 2.0 / entry * 100, 1)),
            (round(entry - atr * 3.0, 6), 40, round(atr * 3.0 / entry * 100, 1)),
        ]
    else:
        dobar_low = round(entry - atr * 1.5, 6)
        dobar_high = round(entry - atr * 0.5, 6)
        stop_loss = round(entry - atr * 2.0, 6)
        tps = [
            (round(entry + atr * 0.8, 6), 90, round(atr * 0.8 / entry * 100, 1)),
            (round(entry + atr * 1.3, 6), 75, round(atr * 1.3 / entry * 100, 1)),
            (round(entry + atr * 2.0, 6), 58, round(atr * 2.0 / entry * 100, 1)),
            (round(entry + atr * 3.0, 6), 40, round(atr * 3.0 / entry * 100, 1)),
        ]

    # Бектест — передаємо вже готовий df
    stats = run_backtest(df, direction)

    # Фільтр слабких стратегій
    if not stats.get('is_valid', False):
        print(f"⛔ {symbol} {timeframe} {direction} — слабка стратегія, пропускаємо")
        return None

    # Оновлюємо ймовірності TP з бектесту
    if stats['count'] > 0:
        tps_with_probs = []
        for i, (tp_price, _, pct) in enumerate(tps):
            prob = stats['tp_probs'][i] if i < len(stats['tp_probs']) else 40
            tps_with_probs.append((tp_price, prob, pct))
        tps = tps_with_probs

    # Визначаємо Tier
    tp1_prob = stats['tp_probs'][0] if stats['count'] > 0 else 50
    if tp1_prob >= 70:
        tier = '🟢'
    elif tp1_prob >= 50:
        tier = '🟡'
    else:
        tier = '🔵'

    return {
        'symbol': symbol.replace('/', ''),
        'timeframe': timeframe,
        'direction': direction,
        'entry': entry,
        'dobar_low': dobar_low,
        'dobar_high': dobar_high,
        'tps': tps,
        'stats': stats,
        'tier': tier,
        'stop_loss': stop_loss,
    }

def scan_all(timeframes=None):
    if timeframes is None:
        timeframes = TIMEFRAMES

    signals = []
    print(f"Сканування ринку {timeframes}...")

    for symbol in WATCHLIST:
        for timeframe in timeframes:
            signal = find_signal(symbol, timeframe)
            if signal:
                print(f"✅ Сигнал знайдено: {signal['symbol']} {timeframe} {signal['direction']}")
                signals.append(signal)

    print(f"Знайдено сигналів: {len(signals)}")
    return signals