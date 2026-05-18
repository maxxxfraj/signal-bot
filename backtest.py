import ccxt
import pandas as pd
import ta

exchange = ccxt.binance()

def get_candles_for_backtest(symbol, timeframe, limit=1000):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except Exception as e:
        print(f"Помилка бектесту {symbol}: {e}")
        return None

def calculate_indicators(df):
    df['ema20'] = ta.trend.ema_indicator(df['close'], window=20)
    df['ema50'] = ta.trend.ema_indicator(df['close'], window=50)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    return df

def run_backtest(symbol, timeframe, direction):
    ccxt_symbol = symbol[:-4] + '/USDT' if symbol.endswith('USDT') else symbol
    df = get_candles_for_backtest(ccxt_symbol, timeframe, limit=1000)

    if df is None or len(df) < 100:
        return default_stats()

    df = calculate_indicators(df)
    df = df.dropna()

    trades = []

    # Перебираємо свічки як потенційні точки входу
    for i in range(50, len(df) - 10):
        row = df.iloc[i]
        price = row['close']
        rsi = row['rsi']
        ema20 = row['ema20']
        ema50 = row['ema50']
        atr = row['atr']

        # Та сама логіка що в scanner.py
        signal = None
        if (price < ema20 and price < ema50 and
                45 < rsi < 60 and row['close'] < row['open']):
            signal = 'SHORT'
        elif (price > ema20 and price > ema50 and
              40 < rsi < 55 and row['close'] > row['open']):
            signal = 'LONG'

        if signal != direction:
            continue

        # Рівні TP для цього входу
        entry = price
        if direction == 'SHORT':
            tp1 = entry - atr * 0.8
            tp2 = entry - atr * 1.3
            tp3 = entry - atr * 2.0
            tp4 = entry - atr * 3.0
        else:
            tp1 = entry + atr * 0.8
            tp2 = entry + atr * 1.3
            tp3 = entry + atr * 2.0
            tp4 = entry + atr * 3.0

        # Симулюємо наступні 20 свічок
        # Для старших таймфреймів дивимось далі вперед
        lookahead = 10 if timeframe in ['1d', '4h'] else 30
        future = df.iloc[i+1:i+1+lookahead]
        max_deviation = 0.0
        reached_tp1 = reached_tp2 = reached_tp3 = reached_tp4 = False

        for _, frow in future.iterrows():
            high = frow['high']
            low = frow['low']

            if direction == 'SHORT':
                # Відхилення проти нас (ціна іде вгору)
                deviation = (high - entry) / entry * 100
                if deviation > max_deviation:
                    max_deviation = deviation

                if low <= tp4:
                    reached_tp4 = True
                if low <= tp3:
                    reached_tp3 = True
                if low <= tp2:
                    reached_tp2 = True
                if low <= tp1:
                    reached_tp1 = True
                    break
            else:
                # Відхилення проти нас (ціна іде вниз)
                deviation = (entry - low) / entry * 100
                if deviation > max_deviation:
                    max_deviation = deviation

                if high >= tp4:
                    reached_tp4 = True
                if high >= tp3:
                    reached_tp3 = True
                if high >= tp2:
                    reached_tp2 = True
                if high >= tp1:
                    reached_tp1 = True
                    break

        trades.append({
            'reached_tp1': reached_tp1,
            'reached_tp2': reached_tp2,
            'reached_tp3': reached_tp3,
            'reached_tp4': reached_tp4,
            'max_deviation': max_deviation,
        })

    if len(trades) == 0:
        return default_stats()

    total = len(trades)
    tp1_hits = sum(1 for t in trades if t['reached_tp1'])
    tp2_hits = sum(1 for t in trades if t['reached_tp2'])
    tp3_hits = sum(1 for t in trades if t['reached_tp3'])
    tp4_hits = sum(1 for t in trades if t['reached_tp4'])

    avg_dev = round(
        sum(t['max_deviation'] for t in trades) / total, 1
    )

    deviations = {}
    for threshold in [1, 2, 3, 5, 10]:
        count = sum(1 for t in trades if t['max_deviation'] >= threshold)
        if count > 0:
            deviations[threshold] = count

    tp1_prob = round(tp1_hits / total * 100)
    tp2_prob = round(tp2_hits / total * 100)
    tp3_prob = round(tp3_hits / total * 100)
    tp4_prob = round(tp4_hits / total * 100)

    return {
        'count': total,
        'avg_dev': avg_dev,
        'deviations': deviations,
        'tp_probs': [tp1_prob, tp2_prob, tp3_prob, tp4_prob],
    }

def default_stats():
    return {
        'count': 0,
        'avg_dev': 0.0,
        'deviations': {},
        'tp_probs': [90, 75, 58, 40],
    }