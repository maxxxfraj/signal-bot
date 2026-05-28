# optimizer.py
import ccxt
import pandas as pd
import numpy as np
import ta
import json
import os
import time
import random
from itertools import product
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing

# Імпортуємо єдине джерело правди та надійний логер до БД
from settings import get_setting, ALL_PAIRS
from database import save_strategy_config_to_db
from edge_validator import QuantitativeEdgeValidator

TIMEFRAMES = ['5m', '15m', '30m', '1h', '4h', '1d']

EMA_FAST_OPTIONS = [8, 10, 12, 15, 20, 25]
EMA_SLOW_OPTIONS = [30, 40, 50, 60, 100]
RSI_MIN_SHORT = [40, 45, 50, 55]
RSI_MAX_SHORT = [60, 65, 70, 75, 80]
RSI_MIN_LONG = [25, 30, 35, 40]
RSI_MAX_LONG = [50, 55, 60, 65]

FEE_RATE = 0.0004
SLIPPAGE = 0.0005


def map_symbol_to_futures(symbol: str) -> str:
    """Автоматично перетворює тикери мем-коїнів під ф'ючерсні стандарти 1000x на Binance"""
    sym = symbol.upper()
    if 'SHIB' in sym:
        return '1000SHIB/USDT:USDT'
    if 'PEPE' in sym:
        return '1000PEPE/USDT:USDT'
    if 'BONK' in sym:
        return '1000BONK/USDT:USDT'
    if 'FLOKI' in sym:
        return '1000FLOKI/USDT:USDT'
    
    # Стандартний формат CCXT
    if '/' not in symbol:
        base = symbol[:-4]
        return f"{base}/USDT:USDT"
    return f"{symbol}:USDT" if not symbol.endswith(":USDT") else symbol


def get_candles_extended(symbol, timeframe, target=10000):
    """Збирає глибоку історію з ф'ючерсів Binance з захистом від шторму ініціалізації"""
    # Додаємо мікро-затримку (Jitter), щоб рівномірно розподілити запити 12 ядер до Binance
    time.sleep(random.uniform(0.1, 2.5))
    
    # Мепимо мем-коїни під ф'ючерсні тикери
    ccxt_futures_symbol = map_symbol_to_futures(symbol)
    
    exchange = ccxt.binanceusdm({'enableRateLimit': True})
    limit_per_request = 1000
    all_ohlcv = []
    since = None
    requests_needed = min(target // limit_per_request, 20)

    for i in range(requests_needed):
        try:
            ohlcv = exchange.fetch_ohlcv(
                ccxt_futures_symbol, timeframe,
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
            time.sleep(0.1)

        except Exception as e:
            print(f"    Помилка завантаження історії {ccxt_futures_symbol} {timeframe}: {e}")
            break

    if not all_ohlcv:
        return None

    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates('timestamp')
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    df.sort_index(inplace=True)
    return df


def calculate_indicators(df, ema_fast, ema_slow):
    df = df.copy()
    df['ema_fast'] = ta.trend.ema_indicator(df['close'], window=ema_fast)
    df['ema_slow'] = ta.trend.ema_indicator(df['close'], window=ema_slow)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['atr'] = ta.volatility.average_true_range(
        df['high'], df['low'], df['close'], window=14
    )
    
    ap = (df['high'] + df['low'] + df['close']) / 3.0
    esa = ta.trend.ema_indicator(ap, window=10)
    d = ta.trend.ema_indicator(abs(ap - esa), window=10)
    d_val = d.copy()
    d_val[d_val == 0] = 0.000001
    ci = (ap - esa) / (0.015 * d_val)
    df['wt1'] = ta.trend.ema_indicator(ci, window=21)
    df['wt2'] = df['wt1'].rolling(window=4).mean()

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

    elif strategy_type == 'wavetrend_bounce':
        dot_level = 45
        if direction == 'SHORT':
            return (prev['wt1'] > prev['wt2'] and 
                    row['wt1'] < row['wt2'] and 
                    row['wt1'] > dot_level)
        else:
            return (prev['wt1'] < prev['wt2'] and 
                    row['wt1'] > row['wt2'] and 
                    row['wt1'] < -dot_level)

    elif strategy_type == 'breakout':
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

    return False


def run_oos_backtest_for_optimization(df, ema_fast, ema_slow, rsi_min, rsi_max, direction, strategy_type, stop_mult, tp1_mult):
    """Швидка симуляція угод на Out-of-Sample вибірці для оптимізатора"""
    trades = []
    for i in range(50, len(df) - 30):
        row = df.iloc[i]
        prev = df.iloc[i-1]
        atr = row['atr']
        if atr == 0 or pd.isna(atr):
            continue

        signal_hit = get_signal(row, prev, ema_fast, ema_slow, rsi_min, rsi_max, direction, strategy_type)
        if not signal_hit:
            continue

        entry = row['close']
        
        if direction == 'SHORT':
            tp1 = entry - atr * tp1_mult
            sl = entry + atr * stop_mult
        else:
            tp1 = entry + atr * tp1_mult
            sl = entry - atr * stop_mult

        future = df.iloc[i+1:i+31]
        tp1_hit = False
        sl_hit = False
        net_pnl = 0.0

        for _, frow in future.iterrows():
            if direction == 'SHORT':
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

        trades.append({'net_pnl': net_pnl})

    return trades


def optimize_symbol_wf(symbol, timeframe, df):
    """
    Проводить Walk-Forward оптимізацію: підбирає параметри окремо для трендової (TREND) 
    та контртрендової (REVERSION) фаз ринку та валідує їх на Out-of-Sample (30%) за допомогою t-статистики.
    """
    validator = QuantitativeEdgeValidator(target_oos_ratio=0.30, fee_rate=FEE_RATE, slippage_pct=SLIPPAGE)
    
    # Спліт даних
    df_is, df_oos = validator.split_data(df)
    
    trend_strategies = ['ema_rsi', 'macd_cross', 'breakout']
    reversion_strategies = ['bb_bounce', 'wavetrend_bounce']
    
    saved_configs = []

    for direction in ['LONG', 'SHORT']:
        rsi_min_opts = RSI_MIN_SHORT if direction == 'SHORT' else RSI_MIN_LONG
        rsi_max_opts = RSI_MAX_SHORT if direction == 'SHORT' else RSI_MAX_LONG

        # 1. ОПТИМІЗАЦІЯ ЯДРА А (TREND ENGINE)
        best_trend_score = -999.0
        best_trend_params = None

        # Трендові мультиплікатори з налаштувань
        stop_mult = get_setting('stop_atr_mult') or 2.0
        tp1_mult = get_setting('tp1_atr_mult') or 0.8

        for strategy_type in trend_strategies:
            for ema_fast, ema_slow in product(EMA_FAST_OPTIONS, EMA_SLOW_OPTIONS):
                if ema_fast >= ema_slow:
                    continue

                for rsi_min, rsi_max in product(rsi_min_opts, rsi_max_opts):
                    if rsi_min >= rsi_max:
                        continue

                    df_is_ind = calculate_indicators(df_is, ema_fast, ema_slow)
                    if len(df_is_ind) < 100:
                        continue

                    is_trades = run_oos_backtest_for_optimization(
                        df_is_ind, ema_fast, ema_slow, rsi_min, rsi_max, direction, strategy_type, stop_mult, tp1_mult
                    )
                    
                    if len(is_trades) < 15:
                        continue
                    
                    is_pnl_array = np.array([t['net_pnl'] for t in is_trades])
                    is_mean = np.mean(is_pnl_array)
                    
                    if is_mean <= 0.15:
                        continue

                    df_oos_ind = calculate_indicators(df_oos, ema_fast, ema_slow)
                    oos_trades = run_oos_backtest_for_optimization(
                        df_oos_ind, ema_fast, ema_slow, rsi_min, rsi_max, direction, strategy_type, stop_mult, tp1_mult
                    )

                    edge_results = validator.evaluate_edge(oos_trades)

                    if edge_results["is_valid_edge"] and edge_results["t_stat"] > best_trend_score:
                        best_trend_score = edge_results["t_stat"]
                        best_trend_params = {
                            'direction': direction,
                            'regime_group': 'TREND',
                            'ema_fast': int(ema_fast),
                            'ema_slow': int(ema_slow),
                            'rsi_min': int(rsi_min),
                            'rsi_max': int(rsi_max),
                            'score': float(edge_results["t_stat"]),
                            'strategy_type': strategy_type
                        }

        if best_trend_params:
            saved_configs.append(best_trend_params)

        # 2. ОПТИМІЗАЦІЯ ЯДРА Б (REVERSION ENGINE)
        best_reversion_score = -999.0
        best_reversion_params = None

        # Жорсткі контртрендові мультиплікатори для роботи в боковику
        stop_mult = 1.0
        tp1_mult = 1.2

        for strategy_type in reversion_strategies:
            for ema_fast, ema_slow in product(EMA_FAST_OPTIONS, EMA_SLOW_OPTIONS):
                if ema_fast >= ema_slow:
                    continue

                for rsi_min, rsi_max in product(rsi_min_opts, rsi_max_opts):
                    if rsi_min >= rsi_max:
                        continue

                    df_is_ind = calculate_indicators(df_is, ema_fast, ema_slow)
                    if len(df_is_ind) < 100:
                        continue

                    is_trades = run_oos_backtest_for_optimization(
                        df_is_ind, ema_fast, ema_slow, rsi_min, rsi_max, direction, strategy_type, stop_mult, tp1_mult
                    )
                    
                    if len(is_trades) < 15:
                        continue
                    
                    is_pnl_array = np.array([t['net_pnl'] for t in is_trades])
                    is_mean = np.mean(is_pnl_array)
                    
                    if is_mean <= 0.15:
                        continue

                    df_oos_ind = calculate_indicators(df_oos, ema_fast, ema_slow)
                    oos_trades = run_oos_backtest_for_optimization(
                        df_oos_ind, ema_fast, ema_slow, rsi_min, rsi_max, direction, strategy_type, stop_mult, tp1_mult
                    )

                    edge_results = validator.evaluate_edge(oos_trades)

                    # Пом'якшені умови валідації виключно для реверсивних стратегій боковика
                    is_valid_reversion = (
                        edge_results["t_stat"] >= 1.5 and 
                        edge_results["expectancy_pct"] >= 0.08 and 
                        edge_results["profit_factor"] >= 1.20
                    )

                    if is_valid_reversion and edge_results["t_stat"] > best_reversion_score:
                        best_reversion_score = edge_results["t_stat"]
                        best_reversion_params = {
                            'direction': direction,
                            'regime_group': 'REVERSION',
                            'ema_fast': int(ema_fast),
                            'ema_slow': int(ema_slow),
                            'rsi_min': int(rsi_min),
                            'rsi_max': int(rsi_max),
                            'score': float(edge_results["t_stat"]),
                            'strategy_type': strategy_type
                        }
        if best_reversion_params:
            saved_configs.append(best_reversion_params)

    return saved_configs if saved_configs else None


def save_strategy_config_to_db_with_retry(symbol, timeframe, direction, regime_group, ema_fast, ema_slow, rsi_min, rsi_max, score, strategy_type, retries=5):
    """Спроба зберегти конфігурацію у PostgreSQL з експоненціальним очікуванням при збоях пулера Neon"""
    for attempt in range(retries):
        try:
            save_strategy_config_to_db(symbol, timeframe, direction, regime_group, ema_fast, ema_slow, rsi_min, rsi_max, score, strategy_type)
            return True
        except Exception as db_err:
            if attempt == retries - 1:
                raise db_err
            # Тимчасова павза для адаптації пулера Neon
            sleep_time = 3.0 * (attempt + 1)
            print(f"⚠️ [DB RETRY] Збій підключення до Neon (Спроба {attempt+1}/{retries}): {db_err}. Повтор за {sleep_time} сек...")
            time.sleep(sleep_time)
    return False


def worker_task(args):
    symbol, timeframe = args
    try:
        df = get_candles_extended(symbol, timeframe, target=8000)
        if df is None or len(df) < 500:
            return symbol, timeframe, None
        
        result = optimize_symbol_wf(symbol, timeframe, df)
        return symbol, timeframe, result
    except Exception as e:
        print(f"Помилка воркера для {symbol} {timeframe}: {e}")
        return symbol, timeframe, None


def run_optimizer():
    print("🚀 Запуск Walk-Forward Optimizer з Out-of-Sample фільтрацією...")
    print(f"Початок: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Кількість пар: {len(ALL_PAIRS)} × Таймфреймів: {len(TIMEFRAMES)}")

    cpu_cores = multiprocessing.cpu_count()
    workers = max(1, cpu_cores // 2)
    print(f"Запуск на {workers} паралельних ядрах...")

    tasks = [(s, tf) for s in ALL_PAIRS for tf in TIMEFRAMES]
    total = len(tasks)
    done = 0
    saved_count = 0

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

                if result:
                    for params in result:
                        # Використовуємо надійну функцію запису з Retry-запобіжником
                        save_strategy_config_to_db_with_retry(
                            symbol=symbol_clean,
                            timeframe=timeframe,
                            direction=params['direction'],
                            regime_group=params['regime_group'],
                            ema_fast=params['ema_fast'],
                            ema_slow=params['ema_slow'],
                            rsi_min=params['rsi_min'],
                            rsi_max=params['rsi_max'],
                            score=params['score'],
                            strategy_type=params['strategy_type']
                        )
                        saved_count += 1
                    print(f"[{done}/{total}] ✅ {symbol} {timeframe} — Оптимальні OOS параметри ({len(result)} конфігурацій) збережено у Neon PostgreSQL!")
                else:
                    print(f"[{done}/{total}] ⛔ {symbol} {timeframe} — стійкої переваги поза вибіркою не знайдено.")

            except Exception as e:
                print(f"[{done}/{total}] ❌ Помилка обробки результату для {symbol} {timeframe}: {e}")

    print(f"\n{'='*50}")
    print(f"✅ Оптимізацію Walk-Forward успішно завершено!")
    print(f"Усього стійких конфігурацій записано в Neon DB: {saved_count}")


if __name__ == '__main__':
    run_optimizer()