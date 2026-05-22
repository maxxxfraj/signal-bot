import pandas as pd
import ta

FEE_RATE = 0.0004      # 0.04%
SLIPPAGE = 0.0005      # 0.05%


def calculate_indicators(df, ema_fast=20, ema_slow=50):
    df = df.copy()
    
    # Розраховуємо динамічні EMA відповідно до параметрів стратегії
    df['ema_fast'] = ta.trend.ema_indicator(df['close'], window=ema_fast)
    df['ema_slow'] = ta.trend.ema_indicator(df['close'], window=ema_slow)

    # Стандартні індикатори для додаткових підтверджень
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['atr'] = ta.volatility.average_true_range(
        df['high'],
        df['low'],
        df['close'],
        window=14
    )

    macd = ta.trend.MACD(
        df['close'],
        window_fast=12,
        window_slow=26,
        window_sign=9
    )
    df['macd'] = macd.macd()
    df['macd_signal'] = macd.macd_signal()

    bb = ta.volatility.BollingerBands(
        df['close'],
        window=20,
        window_dev=2
    )
    df['bb_mid'] = bb.bollinger_mavg()
    df['bb_upper'] = bb.bollinger_hband()
    df['bb_lower'] = bb.bollinger_lband()

    df['volume_ma'] = df['volume'].rolling(window=20).mean()
    df['high_20'] = df['high'].rolling(20).max()
    df['low_20'] = df['low'].rolling(20).min()

    return df


def get_backtest_signal(row, prev, ema_fast, ema_slow, rsi_min, rsi_max, direction, strategy_type):
    """Точне логічне дзеркало генератора сигналів із scanner.py"""
    if strategy_type == 'ema_rsi':
        if direction == 'SHORT':
            trend = row['close'] < row['ema_fast'] and row['close'] < row['ema_slow']
            rsi_ok = rsi_min < row['rsi'] < rsi_max
            candle = row['close'] < row['open']
            ema_cross = row['ema_fast'] < row['ema_slow']
            macd_ok = row['macd'] < row['macd_signal']
            volume_ok = row['volume'] > row['volume_ma'] * 0.7
            bb_ok = row['close'] < row['bb_mid']
            confirmations = sum([ema_cross, macd_ok, volume_ok, bb_ok])
            return trend and rsi_ok and candle and confirmations >= 2
        else:
            trend = row['close'] > row['ema_fast'] and row['close'] > row['ema_slow']
            rsi_ok = rsi_min < row['rsi'] < rsi_max
            candle = row['close'] > row['open']
            ema_cross = row['ema_fast'] > row['ema_slow']
            macd_ok = row['macd'] > row['macd_signal']
            volume_ok = row['volume'] > row['volume_ma'] * 0.7
            bb_ok = row['close'] > row['bb_mid']
            confirmations = sum([ema_cross, macd_ok, volume_ok, bb_ok])
            return trend and rsi_ok and candle and confirmations >= 2

    elif strategy_type == 'macd_cross':
        if direction == 'SHORT':
            return (prev['macd'] > prev['macd_signal'] and
                    row['macd'] < row['macd_signal'] and
                    row['close'] < row['ema_slow'] and
                    rsi_min < row['rsi'] < rsi_max)
        else:
            return (prev['macd'] < prev['macd_signal'] and
                    row['macd'] > row['macd_signal'] and
                    row['close'] > row['ema_slow'] and
                    rsi_min < row['rsi'] < rsi_max)

    elif strategy_type == 'bb_bounce':
        if direction == 'SHORT':
            return (row['close'] > row['bb_upper'] and
                    row['rsi'] > rsi_min and
                    row['close'] < row['open'])
        else:
            return (row['close'] < row['bb_lower'] and
                    row['rsi'] < rsi_max and
                    row['close'] > row['open'])

    elif strategy_type == 'breakout':
        # Неінвертована логіка пробоїв
        if direction == 'LONG':
            return (prev['close'] < prev['high_20'] and
                    row['close'] > prev['high_20'] and
                    row['volume'] > row['volume_ma'] * 1.5 and
                    row['rsi'] > rsi_min)
        else:
            return (prev['close'] > prev['low_20'] and
                    row['close'] < prev['low_20'] and
                    row['volume'] > row['volume_ma'] * 1.5 and
                    row['rsi'] < rsi_max)

    elif strategy_type == 'vol_spike':
        if direction == 'SHORT':
            return (row['volume'] > row['volume_ma'] * 3 and
                    row['close'] < row['open'] and
                    row['rsi'] > rsi_min)
        else:
            return (row['volume'] > row['volume_ma'] * 3 and
                    row['close'] > row['open'] and
                    row['rsi'] < rsi_max)

    elif strategy_type == 'mean_reversion':
        atr = row['atr']
        if direction == 'SHORT':
            return (row['close'] > row['bb_upper'] + atr * 0.5 and
                    row['rsi'] > rsi_min)
        else:
            return (row['close'] < row['bb_lower'] - atr * 0.5 and
                    row['rsi'] < rsi_max)

    elif strategy_type == 'rsi_div':
        if direction == 'SHORT':
            return (row['close'] > prev['close'] and
                    row['rsi'] < prev['rsi'] and
                    row['close'] > row['ema_slow'] and
                    row['rsi'] > rsi_min)
        else:
            return (row['close'] < prev['close'] and
                    row['rsi'] > prev['rsi'] and
                    row['close'] < row['ema_slow'] and
                    row['rsi'] < rsi_max)

    return False


def run_backtest(df, direction, ema_fast=20, ema_slow=50, rsi_min=32, rsi_max=60, 
                 strategy_type='ema_rsi', stop_mult=2.0, tp1_mult=0.8, min_trades=12, min_prob=55):
    try:
        direction = direction.upper()
        df = df.copy()

        # Розраховуємо індикатори під конкретні параметри стратегії
        df = calculate_indicators(df, ema_fast, ema_slow)
        df = df.dropna()

        if len(df) < 100:
            return default_stats()

        trades = []

        for i in range(50, len(df) - 30):
            row = df.iloc[i]
            prev = df.iloc[i - 1]

            atr = row['atr']
            if pd.isna(atr) or atr == 0:
                continue

            # Визначаємо сигнал на основі активної стратегії
            is_signal = get_backtest_signal(row, prev, ema_fast, ema_slow, 
                                            rsi_min, rsi_max, direction, strategy_type)
            if not is_signal:
                continue

            entry = row['close']

            # Розрахунок динамічного ризик-менеджменту
            if direction == 'SHORT':
                stop_loss = entry + atr * stop_mult
                tp1 = entry - atr * tp1_mult
                tp2 = entry - atr * (tp1_mult + 0.5)
                tp3 = entry - atr * (tp1_mult + 1.2)
                tp4 = entry - atr * (tp1_mult + 2.2)
            else:
                stop_loss = entry - atr * stop_mult
                tp1 = entry + atr * tp1_mult
                tp2 = entry + atr * (tp1_mult + 0.5)
                tp3 = entry + atr * (tp1_mult + 1.2)
                tp4 = entry + atr * (tp1_mult + 2.2)

            lookahead = 30
            future = df.iloc[i + 1 : i + 1 + lookahead]

            max_deviation = 0.0
            reached_tp1 = False
            reached_tp2 = False
            reached_tp3 = False
            reached_tp4 = False
            hit_sl = False
            net_pnl = 0.0

            for _, frow in future.iterrows():
                high = frow['high']
                low = frow['low']

                if direction == 'SHORT':
                    # Пріоритет стоп-лоссу при тестуванні
                    if high >= stop_loss:
                        hit_sl = True
                        gross_loss = (stop_loss - entry) / entry
                        net_pnl = -(gross_loss + (FEE_RATE * 2) + SLIPPAGE) * 100
                        break

                    deviation = ((high - entry) / entry) * 100
                    if deviation > max_deviation:
                        max_deviation = deviation

                    if low <= tp1:
                        reached_tp1 = True
                        if low <= tp2:
                            reached_tp2 = True
                            if low <= tp3:
                                reached_tp3 = True
                                if low <= tp4:
                                    reached_tp4 = True

                        gross_profit = (entry - tp1) / entry
                        net_pnl = (gross_profit - (FEE_RATE * 2) - SLIPPAGE) * 100
                        break
                else:
                    if low <= stop_loss:
                        hit_sl = True
                        gross_loss = (entry - stop_loss) / entry
                        net_pnl = -(gross_loss + (FEE_RATE * 2) + SLIPPAGE) * 100
                        break

                    deviation = ((entry - low) / entry) * 100
                    if deviation > max_deviation:
                        max_deviation = deviation

                    if high >= tp1:
                        reached_tp1 = True
                        if high >= tp2:
                            reached_tp2 = True
                            if high >= tp3:
                                reached_tp3 = True
                                if high >= tp4:
                                    reached_tp4 = True

                        gross_profit = (tp1 - entry) / entry
                        net_pnl = (gross_profit - (FEE_RATE * 2) - SLIPPAGE) * 100
                        break

            trades.append({
                'reached_tp1': reached_tp1,
                'reached_tp2': reached_tp2,
                'reached_tp3': reached_tp3,
                'reached_tp4': reached_tp4,
                'hit_sl': hit_sl,
                'max_deviation': max_deviation,
                'net_pnl': net_pnl,
            })

        if len(trades) == 0:
            return default_stats()

        total = len(trades)
        tp1_hits = sum(1 for t in trades if t['reached_tp1'])
        tp2_hits = sum(1 for t in trades if t['reached_tp2'])
        tp3_hits = sum(1 for t in trades if t['reached_tp3'])
        tp4_hits = sum(1 for t in trades if t['reached_tp4'])
        sl_hits = sum(1 for t in trades if t['hit_sl'])

        avg_dev = round(sum(t['max_deviation'] for t in trades) / total, 2)
        avg_pnl = round(sum(t['net_pnl'] for t in trades) / total, 2)
        stop_rate = round(sl_hits / total * 100, 1)

        tp1_prob = round(tp1_hits / total * 100)
        tp2_prob = round(tp2_hits / total * 100)
        tp3_prob = round(tp3_hits / total * 100)
        tp4_prob = round(tp4_hits / total * 100)

        return {
            'count': total,
            'avg_dev': avg_dev,
            'avg_pnl': avg_pnl,
            'stop_rate': stop_rate,
            'tp_probs': [tp1_prob, tp2_prob, tp3_prob, tp4_prob],
            'is_valid': (
            total >= min_trades and
            tp1_prob >= min_prob and  # <--- Тепер тут стоїть динамічний параметр!
            avg_pnl > 0
            ),
        }

    except Exception as e:
        print(f"Помилка бектесту: {e}")
        return default_stats()


def default_stats():
    return {
        'count': 0,
        'avg_dev': 0.0,
        'avg_pnl': 0.0,
        'stop_rate': 0.0,
        'tp_probs': [0, 0, 0, 0],
        'is_valid': False,
    }