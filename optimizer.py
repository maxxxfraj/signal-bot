import ccxt
import pandas as pd
import numpy as np
import ta
import json
import os
import time
from itertools import product
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing

WATCHLIST = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT',
    'XRP/USDT', 'DOGE/USDT', 'ADA/USDT', 'AVAX/USDT',
    'DOT/USDT', 'POL/USDT', 'LINK/USDT', 'UNI/USDT',
    'ATOM/USDT', 'LTC/USDT', 'ETC/USDT',
]

TIMEFRAMES = ['5m', '15m', '30m', '1h', '4h', '1d']
CONFIG_FILE = 'strategy_config.json'

EMA_FAST_OPTIONS = [8, 10, 12, 15, 20, 25]
EMA_SLOW_OPTIONS = [30, 40, 50, 60, 100]
RSI_MIN_SHORT = [40, 45, 50, 55]
RSI_MAX_SHORT = [60, 65, 70, 75, 80]
RSI_MIN_LONG = [25, 30, 35, 40]
RSI_MAX_LONG = [50, 55, 60, 65]


def get_candles_extended(symbol, timeframe, target=10000):
    exchange = ccxt.binance({'enableRateLimit': True})
    limit_per_request = 1000
    all_ohlcv = []
    since = None
    requests_needed = min(target // limit_per_request, 20)

    for i in range(requests_needed):
        try:
            ohlcv = exchange.fetch_ohlcv(
                symbol, timeframe,
                limit=limit_per_request,
                since=since
            )
            if not ohlcv:
                break

            if since is None:
                all_ohlcv = ohlcv
            else:
                all_ohlcv = ohlcv + all_ohlcv

            since = ohlcv[0][0] - (ohlcv[1][0] - ohlcv[0][0])
            time.sleep(0.2)

        except Exception as e:
            print(f"    Помилка запиту {i+1}: {e}")
            break

    if not all_ohlcv:
        return None

    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates('timestamp')
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    df.sort_index(inplace=True)
    return df


def save_config_safely(data, filepath=CONFIG_FILE):
    tmp_path = filepath + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, filepath)
        return True
    except Exception as e:
        print(f"Помилка збереження конфігу: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def calculate_indicators(df, ema_fast, ema_slow):
    df = df.copy()
    df['ema_fast'] = ta.trend.ema_indicator(df['close'], window=ema_fast)
    df['ema_slow'] = ta.trend.ema_indicator(df['close'], window=ema_slow)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['atr'] = ta.volatility.average_true_range(
        df['high'], df['low'], df['close'], window=14
    )
    macd = ta.trend.MACD(df['close'])
    df['macd'] = macd.macd()
    df['macd_signal'] = macd.macd_signal()
    bb = ta.volatility.BollingerBands(df['close'])
    df['bb_mid'] = bb.bollinger_mavg()
    df['bb_upper'] = bb.bollinger_hband()
    df['bb_lower'] = bb.bollinger_lband()
    df['volume_ma'] = df['volume'].rolling(20).mean()
    df['high_20'] = df['high'].rolling(20).max()
    df['low_20'] = df['low'].rolling(20).min()
    return df.dropna()


def get_signal(row, prev, ema_fast, ema_slow, rsi_min, rsi_max, direction, strategy_type):
    if strategy_type == 'ema_rsi':
        if direction == 'SHORT':
            return (row['close'] < row['ema_fast'] and
                    row['close'] < row['ema_slow'] and
                    rsi_min < row['rsi'] < rsi_max and
                    row['close'] < row['open'])
        else:
            return (row['close'] > row['ema_fast'] and
                    row['close'] > row['ema_slow'] and
                    rsi_min < row['rsi'] < rsi_max and
                    row['close'] > row['open'])

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
        if direction == 'SHORT':
            return (prev['close'] < prev['high_20'] and
                    row['close'] > row['high_20'] and
                    row['volume'] > row['volume_ma'] * 1.5 and
                    row['rsi'] > rsi_min)
        else:
            return (prev['close'] > prev['low_20'] and
                    row['close'] < row['low_20'] and
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


def backtest_strategy(df, ema_fast, ema_slow, rsi_min, rsi_max, direction, strategy_type):
    FEE_RATE = 0.0004
    SLIPPAGE = 0.0005

    df = calculate_indicators(df, ema_fast, ema_slow)
    if len(df) < 100:
        return None

    trades = []

    for i in range(50, len(df) - 30):
        row = df.iloc[i]
        prev = df.iloc[i-1]
        atr = row['atr']
        if atr == 0 or pd.isna(atr):
            continue

        signal = get_signal(row, prev, ema_fast, ema_slow,
                            rsi_min, rsi_max, direction, strategy_type)
        if not signal:
            continue

        entry = row['close']
        if direction == 'SHORT':
            tp1 = entry - atr * 0.8
            sl = entry + atr * 2.0
        else:
            tp1 = entry + atr * 0.8
            sl = entry - atr * 2.0

        future = df.iloc[i+1:i+31]
        tp1_hit = False
        sl_hit = False
        max_dev = 0.0
        net_pnl = 0.0

        for _, frow in future.iterrows():
            if direction == 'SHORT':
                dev = (frow['high'] - entry) / entry * 100
                if dev > max_dev:
                    max_dev = dev
                # Стоп першим
                if frow['high'] >= sl:
                    sl_hit = True
                    gross_loss = (sl - entry) / entry
                    net_pnl = -(gross_loss + FEE_RATE * 2 + SLIPPAGE) * 100
                    break
                if frow['low'] <= tp1:
                    tp1_hit = True
                    gross_profit = (entry - tp1) / entry
                    net_pnl = (gross_profit - FEE_RATE * 2 - SLIPPAGE) * 100
                    break
            else:
                dev = (entry - frow['low']) / entry * 100
                if dev > max_dev:
                    max_dev = dev
                # Стоп першим
                if frow['low'] <= sl:
                    sl_hit = True
                    gross_loss = (entry - sl) / entry
                    net_pnl = -(gross_loss + FEE_RATE * 2 + SLIPPAGE) * 100
                    break
                if frow['high'] >= tp1:
                    tp1_hit = True
                    gross_profit = (tp1 - entry) / entry
                    net_pnl = (gross_profit - FEE_RATE * 2 - SLIPPAGE) * 100
                    break

        trades.append({
            'tp1_hit': tp1_hit,
            'sl_hit': sl_hit,
            'max_dev': max_dev,
            'net_pnl': net_pnl,
        })

    if len(trades) < 10:
        return None

    total = len(trades)
    tp1_count = sum(1 for t in trades if t['tp1_hit'])
    sl_count = sum(1 for t in trades if t['sl_hit'])
    tp1_rate = tp1_count / total
    sl_rate = sl_count / total
    avg_dev = sum(t['max_dev'] for t in trades) / total
    avg_pnl = sum(t['net_pnl'] for t in trades) / total

    profit = tp1_count * 0.8
    loss = sl_count * 2.0
    profit_factor = profit / loss if loss > 0 else 999

    score = tp1_rate * 100 - sl_rate * 50 - avg_dev * 2

    return {
        'total': total,
        'tp1_rate': round(tp1_rate * 100, 1),
        'sl_rate': round(sl_rate * 100, 1),
        'avg_dev': round(avg_dev, 2),
        'avg_pnl': round(avg_pnl, 2),
        'profit_factor': round(profit_factor, 2),
        'score': round(float(score), 2),
        'is_valid': (tp1_rate * 100 >= 60 and profit_factor >= 1.5 and avg_pnl > 0),
    }


def validate_strategy(df, ema_fast, ema_slow, rsi_min, rsi_max, direction, strategy_type):
    if len(df) < 300:
        return False, 0

    mid = len(df) // 2
    r1 = backtest_strategy(df.iloc[:mid], ema_fast, ema_slow,
                           rsi_min, rsi_max, direction, strategy_type)
    r2 = backtest_strategy(df.iloc[mid:], ema_fast, ema_slow,
                           rsi_min, rsi_max, direction, strategy_type)

    if not r1 or not r2:
        return False, 0
    if r1['tp1_rate'] < 55 or r2['tp1_rate'] < 55:
        return False, 0

    fresh_start = int(len(df) * 0.8)
    r_fresh = backtest_strategy(df.iloc[fresh_start:], ema_fast, ema_slow,
                                rsi_min, rsi_max, direction, strategy_type)
    if not r_fresh or r_fresh['tp1_rate'] < 50:
        return False, 0

    period_size = len(df) // 4
    good_periods = 0
    for p in range(4):
        df_period = df.iloc[p*period_size:(p+1)*period_size]
        r_period = backtest_strategy(df_period, ema_fast, ema_slow,
                                     rsi_min, rsi_max, direction, strategy_type)
        if r_period and r_period['tp1_rate'] >= 50:
            good_periods += 1

    if good_periods < 3:
        return False, 0

    r_full = backtest_strategy(df, ema_fast, ema_slow,
                               rsi_min, rsi_max, direction, strategy_type)
    if not r_full or not r_full['is_valid']:
        return False, 0

    return True, r_full['score']


def optimize_symbol(symbol, timeframe, df):
    best_results = {}
    strategy_types = [
        'ema_rsi', 'macd_cross', 'bb_bounce',
        'breakout', 'vol_spike', 'mean_reversion', 'rsi_div'
    ]

    for direction in ['LONG', 'SHORT']:
        best_score = -999
        best_params = None

        rsi_min_opts = RSI_MIN_SHORT if direction == 'SHORT' else RSI_MIN_LONG
        rsi_max_opts = RSI_MAX_SHORT if direction == 'SHORT' else RSI_MAX_LONG

        for strategy_type in strategy_types:
            for ema_fast, ema_slow in product(EMA_FAST_OPTIONS, EMA_SLOW_OPTIONS):
                if ema_fast >= ema_slow:
                    continue

                for rsi_min, rsi_max in product(rsi_min_opts, rsi_max_opts):
                    if rsi_min >= rsi_max:
                        continue

                    valid, score = validate_strategy(
                        df, ema_fast, ema_slow,
                        rsi_min, rsi_max, direction, strategy_type
                    )

                    if valid and score > best_score:
                        best_score = score
                        best_params = {
                            'ema_fast': int(ema_fast),
                            'ema_slow': int(ema_slow),
                            'rsi_min': int(rsi_min),
                            'rsi_max': int(rsi_max),
                            'score': float(score),
                            'strategy_type': strategy_type,
                        }

        if best_params:
            best_results[direction] = best_params

    return best_results if best_results else None


def worker_task(args):
    symbol, timeframe = args
    try:
        df = get_candles_extended(symbol, timeframe, target=10000)
        if df is None or len(df) < 300:
            return symbol, timeframe, None
        result = optimize_symbol(symbol, timeframe, df)
        return symbol, timeframe, result
    except Exception as e:
        print(f"Помилка worker {symbol} {timeframe}: {e}")
        return symbol, timeframe, None


def run_optimizer():
    print("🚀 Запуск паралельного оптимізатора (10000 свічок)...")
    print(f"Час: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Пар: {len(WATCHLIST)} × Таймфреймів: {len(TIMEFRAMES)} = {len(WATCHLIST)*len(TIMEFRAMES)} задач")

    cpu_cores = multiprocessing.cpu_count()
    workers = max(1, cpu_cores // 2)
    print(f"CPU ядер: {cpu_cores}, використовуємо: {workers}")
    print()

    config = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except Exception:
            config = {}

    tasks = [(s, tf) for s in WATCHLIST for tf in TIMEFRAMES]
    total = len(tasks)
    done = 0
    found_long = 0
    found_short = 0
    strategy_counts = {}

    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_to_task = {
            executor.submit(worker_task, task): task
            for task in tasks
        }

        for future in as_completed(future_to_task):
            done += 1
            try:
                symbol, timeframe, result = future.result()
                symbol_clean = symbol.replace('/', '')

                if symbol_clean not in config:
                    config[symbol_clean] = {}

                if result:
                    config[symbol_clean][timeframe] = result

                    if 'LONG' in result:
                        found_long += 1
                        st = result['LONG'].get('strategy_type', 'unknown')
                        strategy_counts[st] = strategy_counts.get(st, 0) + 1
                        print(f"[{done}/{total}] ✅ {symbol} {timeframe} LONG "
                              f"[{result['LONG']['strategy_type']}] "
                              f"score={result['LONG']['score']:.1f}")

                    if 'SHORT' in result:
                        found_short += 1
                        st = result['SHORT'].get('strategy_type', 'unknown')
                        strategy_counts[st] = strategy_counts.get(st, 0) + 1
                        print(f"[{done}/{total}] ✅ {symbol} {timeframe} SHORT "
                              f"[{result['SHORT']['strategy_type']}] "
                              f"score={result['SHORT']['score']:.1f}")
                else:
                    print(f"[{done}/{total}] ⛔ {symbol} {timeframe} — немає стратегії")

            except Exception as e:
                print(f"[{done}/{total}] ❌ Помилка: {e}")

            # Зберігаємо прогрес кожні 10 задач
            if done % 10 == 0:
                config['updated_at'] = datetime.now().isoformat()
                save_config_safely(config)
                print(f"💾 Прогрес збережено ({done}/{total})")

    config['updated_at'] = datetime.now().isoformat()
    save_config_safely(config)

    print(f"\n{'='*50}")
    print(f"✅ Оптимізацію завершено!")
    print(f"LONG стратегій: {found_long}")
    print(f"SHORT стратегій: {found_short}")
    print(f"Всього: {found_long + found_short}")
    print(f"По типах: {strategy_counts}")
    print(f"Збережено в {CONFIG_FILE}")


if __name__ == '__main__':
    run_optimizer()