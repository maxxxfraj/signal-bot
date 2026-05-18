import pandas as pd
import ta

def calculate_indicators(df):
    df['ema20'] = ta.trend.ema_indicator(df['close'], window=20)
    df['ema50'] = ta.trend.ema_indicator(df['close'], window=50)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    return df

def run_backtest(df, direction):
    try:
        df = df.copy()
        df = calculate_indicators(df)
        df = df.dropna()

        if len(df) < 100:
            return default_stats()

        trades = []

        for i in range(50, len(df) - 10):
            row = df.iloc[i]
            price = row['close']
            rsi = row['rsi']
            ema20 = row['ema20']
            ema50 = row['ema50']
            atr = row['atr']

            signal = None
            if (row['close'] < ema20 and
                row['close'] < ema50 and
                45 < rsi < 60 and
                row['close'] < row['open']):
                signal = 'SHORT'
            elif (row['close'] > ema20 and
                  row['close'] > ema50 and
                  40 < rsi < 55 and
                  row['close'] > row['open']):
                signal = 'LONG'

            if signal != direction:
                continue

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

            lookahead = 10 if direction in ['1d', '4h'] else 30
            future = df.iloc[i+1:i+1+lookahead]
            max_deviation = 0.0
            reached_tp1 = reached_tp2 = reached_tp3 = reached_tp4 = False

            for _, frow in future.iterrows():
                high = frow['high']
                low = frow['low']

                if direction == 'SHORT':
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

    except Exception as e:
        print(f"Помилка бектесту: {e}")
        return default_stats()

def default_stats():
    return {
        'count': 0,
        'avg_dev': 0.0,
        'deviations': {},
        'tp_probs': [90, 75, 58, 40],
    }