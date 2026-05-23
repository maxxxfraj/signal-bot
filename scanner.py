import ccxt.async_support as ccxt
import pandas as pd
import ta
import os
import json
import asyncio
from backtest import run_backtest
from settings import get_setting, get_exchange_client  # Додано імпорт клієнта

# Ініціалізація динамічного асинхронного клієнта біржі (Binance або MEXC)
exchange = get_exchange_client(async_mode=True)

CONFIG_FILE = 'strategy_config.json'
_config_cache = {}
_config_mtime = 0

# Створюємо глобальний асинхронний замок для завантаження ринків
_load_markets_lock = asyncio.Lock()

# Безпечні дефолтні параметри
SAFE_DEFAULTS = {
    'SHORT': {
        'ema_fast': 20, 'ema_slow': 50,
        'rsi_min': 40, 'rsi_max': 68,
        'strategy_type': 'ema_rsi',
    },
    'LONG': {
        'ema_fast': 20, 'ema_slow': 50,
        'rsi_min': 32, 'rsi_max': 60,
        'strategy_type': 'ema_rsi',
    },
}


def load_strategy_config():
    global _config_cache, _config_mtime
    try:
        if not os.path.exists(CONFIG_FILE):
            return {}
        mtime = os.path.getmtime(CONFIG_FILE)
        if mtime != _config_mtime:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                _config_cache = json.load(f)
            _config_mtime = mtime
            print(f"🔄 Конфіг стратегій оновлено")
        return _config_cache
    except Exception as e:
        print(f"⚠️ Помилка читання конфігу: {e} — використовую defaults")
        return {}


def get_params(symbol, timeframe, direction):
    config = load_strategy_config()
    symbol_clean = symbol.replace('/', '')
    try:
        params = config[symbol_clean][timeframe][direction]
        return (
            params['ema_fast'],
            params['ema_slow'],
            params['rsi_min'],
            params['rsi_max'],
            params.get('strategy_type', 'ema_rsi'),
        )
    except (KeyError, TypeError):
        d = SAFE_DEFAULTS[direction]
        return d['ema_fast'], d['ema_slow'], d['rsi_min'], d['rsi_max'], d['strategy_type']


async def get_candles(symbol, timeframe, limit=1000):
    global _load_markets_lock
    try:
        if not exchange.markets:
            async with _load_markets_lock:
                if not exchange.markets:
                    print(f"⏳ Попереднє завантаження інформації про ринки {get_setting('exchange_name').upper()}...")
                    await exchange.load_markets()
                    print("✅ Інформацію про ринки успішно завантажено та закешовано!")

        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except Exception as e:
        print(f"Помилка отримання свічок {symbol} {timeframe}: {e}")
        return None


def calculate_indicators(df):
    # Розрахунок базових індикаторів
    df['ema20'] = ta.trend.ema_indicator(df['close'], window=20)
    df['ema50'] = ta.trend.ema_indicator(df['close'], window=50)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)

    macd = ta.trend.MACD(df['close'], window_fast=12, window_slow=26, window_sign=9)
    df['macd'] = macd.macd()
    df['macd_signal'] = macd.macd_signal()

    df['atr'] = ta.volatility.average_true_range(
        df['high'], df['low'], df['close'], window=14
    )

    bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
    df['bb_upper'] = bb.bollinger_hband()
    df['bb_lower'] = bb.bollinger_lband()
    df['bb_mid'] = bb.bollinger_mavg()

    df['volume_ma'] = df['volume'].rolling(window=20).mean()
    df['high_20'] = df['high'].rolling(20).max()
    df['low_20'] = df['low'].rolling(20).min()

    return df


def check_signal_by_type(last, prev, df, symbol, timeframe, direction):
    ema_fast, ema_slow, rsi_min, rsi_max, strategy_type = get_params(
        symbol, timeframe, direction
    )

    ema_f = ta.trend.ema_indicator(df['close'], window=ema_fast).iloc[-2]
    ema_s = ta.trend.ema_indicator(df['close'], window=ema_slow).iloc[-2]

    if strategy_type == 'ema_rsi':
        if direction == 'SHORT':
            trend = last['close'] < ema_f and last['close'] < ema_s
            rsi_ok = rsi_min < last['rsi'] < rsi_max
            candle = last['close'] < last['open']
            ema_cross = ema_f < ema_s
            macd_ok = last['macd'] < last['macd_signal']
            volume_ok = last['volume'] > last['volume_ma'] * 0.7
            bb_ok = last['close'] < last['bb_mid']
            confirmations = sum([ema_cross, macd_ok, volume_ok, bb_ok])
            return trend and rsi_ok and candle and confirmations >= 2
        else:
            trend = last['close'] > ema_f and last['close'] > ema_s
            rsi_ok = rsi_min < last['rsi'] < rsi_max
            candle = last['close'] > last['open']
            ema_cross = ema_f > ema_s
            macd_ok = last['macd'] > last['macd_signal']
            volume_ok = last['volume'] > last['volume_ma'] * 0.7
            bb_ok = last['close'] > last['bb_mid']
            confirmations = sum([ema_cross, macd_ok, volume_ok, bb_ok])
            return trend and rsi_ok and candle and confirmations >= 2

    elif strategy_type == 'macd_cross':
        if direction == 'SHORT':
            return (prev['macd'] > prev['macd_signal'] and
                    last['macd'] < last['macd_signal'] and
                    last['close'] < ema_s and
                    rsi_min < last['rsi'] < rsi_max)
        else:
            return (prev['macd'] < prev['macd_signal'] and
                    last['macd'] > last['macd_signal'] and
                    last['close'] > ema_s and
                    rsi_min < last['rsi'] < rsi_max)

    elif strategy_type == 'bb_bounce':
        if direction == 'SHORT':
            return (last['close'] > last['bb_upper'] and
                    last['rsi'] > rsi_min and
                    last['close'] < last['open'])
        else:
            return (last['close'] < last['bb_lower'] and
                    last['rsi'] < rsi_max and
                    last['close'] > last['open'])

    elif strategy_type == 'breakout':
        high_20 = df['high'].rolling(20).max().iloc[-2]
        low_20 = df['low'].rolling(20).min().iloc[-2]
        if direction == 'LONG':  # Виправлена неінвертована логіка
            return (prev['close'] < high_20 and
                    last['close'] > high_20 and
                    last['volume'] > last['volume_ma'] * 1.5 and
                    last['rsi'] > rsi_min)
        else:
            return (prev['close'] > low_20 and
                    last['close'] < low_20 and
                    last['volume'] > last['volume_ma'] * 1.5 and
                    last['rsi'] < rsi_max)

    elif strategy_type == 'vol_spike':
        if direction == 'SHORT':
            return (last['volume'] > last['volume_ma'] * 3 and
                    last['close'] < last['open'] and
                    last['rsi'] > rsi_min)
        else:
            return (last['volume'] > last['volume_ma'] * 3 and
                    last['close'] > last['open'] and
                    last['rsi'] < rsi_max)

    elif strategy_type == 'mean_reversion':
        atr = last['atr']
        if direction == 'SHORT':
            return (last['close'] > last['bb_upper'] + atr * 0.5 and
                    last['rsi'] > rsi_min)
        else:
            return (last['close'] < last['bb_lower'] - atr * 0.5 and
                    last['rsi'] < rsi_max)

    elif strategy_type == 'rsi_div':
        if direction == 'SHORT':
            return (last['close'] > prev['close'] and
                    last['rsi'] < prev['rsi'] and
                    last['close'] > ema_s and
                    last['rsi'] > rsi_min)
        else:
            return (last['close'] < prev['close'] and
                    last['rsi'] > prev['rsi'] and
                    last['close'] < ema_s and
                    last['rsi'] < rsi_max)

    return False


def log_skip(msg, scan_logs=None):
    """Службовий помічник для запису в консоль та накопичувач логів"""
    print(msg)
    if scan_logs is not None:
        scan_logs.append(msg)


async def check_higher_tf_bias(symbol, timeframe, direction, scan_logs=None):
    if not get_setting('htf_bias_enabled'):
        return True

    htf_diff_threshold = get_setting('htf_diff_threshold')

    try:
        higher_tf_map = {
            '5m': '15m',
            '15m': '1h',
            '1h': '4h',
            '4h': '1d'
        }

        if timeframe not in higher_tf_map:
            return True

        higher_tf = higher_tf_map[timeframe]

        df = await get_candles(symbol, higher_tf, limit=200)

        if df is None or len(df) < 50:
            return True

        df['ema20'] = ta.trend.ema_indicator(df['close'], window=20)
        df['ema50'] = ta.trend.ema_indicator(df['close'], window=50)

        latest = df.iloc[-1]
        ema20 = latest['ema20']
        ema50 = latest['ema50']

        diff_pct = abs((ema20 - ema50) / ema50) * 100

        if diff_pct < htf_diff_threshold:
            log_skip(f"⛔ {symbol} {timeframe} {direction} — EMA тренд старшого ТФ занадто слабкий ({diff_pct:.2f}% < {htf_diff_threshold}%)", scan_logs)
            return False

        if direction == 'LONG':
            is_ok = ema20 > ema50
            if not is_ok:
                log_skip(f"⛔ {symbol} {timeframe} LONG — тренд старшого ТФ {higher_tf} ведмежий (EMA20 < EMA50)", scan_logs)
            return is_ok
        elif direction == 'SHORT':
            is_ok = ema20 < ema50
            if not is_ok:
                log_skip(f"⛔ {symbol} {timeframe} SHORT — тренд старшого ТФ {higher_tf} бичачий (EMA20 > EMA50)", scan_logs)
            return is_ok

        return True

    except Exception as e:
        print(f"HTF bias error {symbol} {timeframe}: {e}")
        return True


async def check_btc_and_correlation(symbol, timeframe, df_alt, direction, scan_logs=None, btc_df=None):
    """Розрахунок лінійної кореляції Пірсона до BTC та трендовий фільтр Біткоїна (з підтримкою кешування)"""
    btc_filt = get_setting('btc_filter_enabled')
    if btc_filt is None:
        btc_filt = True
        
    try:
        # ОПТИМІЗАЦІЯ: Якщо свічки BTC вже завантажені в глобальний кеш scan_all(), ми не робимо повторних запитів до мережі!
        df_btc = btc_df
        if df_btc is None:
            df_btc = await get_candles('BTC/USDT', timeframe, limit=len(df_alt))
            
        if df_btc is None or len(df_btc) < 50:
            return True, 0.0

        # Розрахунок кореляції Пірсона через Pandas (з вирівнюванням індексів) [4]
        df_alt_aligned, df_btc_aligned = df_alt.align(df_btc, join='inner', axis=0)
        returns_alt = df_alt_aligned['close'].pct_change().dropna()
        returns_btc = df_btc_aligned['close'].pct_change().dropna()
        
        correlation = returns_alt.corr(returns_btc)
        if pd.isna(correlation):
            correlation = 0.0

        # Трендовий фільтр BTC
        if btc_filt:
            ema20_btc = ta.trend.ema_indicator(df_btc['close'], window=20).iloc[-1]
            ema50_btc = ta.trend.ema_indicator(df_btc['close'], window=50).iloc[-1]
            
            btc_bullish = ema20_btc > ema50_btc
            
            if direction == 'LONG' and not btc_bullish:
                log_skip(f"⛔ {symbol} — фільтр BTC заблокував LONG (BTC у спадному тренді)", scan_logs)
                return False, correlation
            elif direction == 'SHORT' and btc_bullish:
                log_skip(f"⛔ {symbol} — фільтр BTC заблокував SHORT (BTC у висхідному тренді)", scan_logs)
                return False, correlation

        return True, round(correlation, 2)
    except Exception as e:
        print(f"Помилка розрахунку метрик BTC для {symbol}: {e}")
        return True, 0.0


async def find_signal(symbol, timeframe, scan_logs=None, btc_df=None):
    # Динамічно збільшуємо ліміт свічок для дрібних таймфреймів
    limit = 1000
    if timeframe == '5m':
        limit = 6000   # ~20 днів історії для 5m, щоб назбирати угоди для бектесту
    elif timeframe == '15m':
        limit = 4000   # ~41 день історії для 15m

    df = await get_candles(symbol, timeframe, limit=limit)
    if df is None or len(df) < 60:
        return None

    # Асинхронний виклик важких розрахунків індикаторів
    df = await asyncio.to_thread(calculate_indicators, df)
    df = df.dropna()

    if len(df) < 3:
        return None

    last = df.iloc[-2]
    prev = df.iloc[-3]
    current = df.iloc[-1]

    price = current['close']
    atr = last['atr']

    if pd.isna(atr) or atr == 0:
        return None

    direction = None

    if check_signal_by_type(last, prev, df, symbol, timeframe, 'SHORT'):
        direction = 'SHORT'
    elif check_signal_by_type(last, prev, df, symbol, timeframe, 'LONG'):
        direction = 'LONG'

    if direction is None:
        return None

    # 1. Фільтр ринкового режиму ADX (Trending vs flat)
    regime_filter = get_setting('regime_filter_enabled')
    if regime_filter is None:
        regime_filter = True
        
    if regime_filter:
        try:
            # Отримуємо тип стратегії сигналу
            _, _, _, _, strategy_type = get_params(symbol, timeframe, direction)
            
            # Рахуємо силу тренду через ADX
            adx_series = ta.trend.adx(df['high'], df['low'], df['close'], window=14)
            latest_adx = adx_series.iloc[-1]
            
            trend_strategies = ['ema_rsi', 'macd_cross', 'breakout', 'vol_spike']
            mean_reversion_strategies = ['bb_bounce', 'mean_reversion']
            
            if strategy_type in trend_strategies and latest_adx < 20:
                log_skip(f"⛔ {symbol} {timeframe} {direction} — ринок у флеті (ADX={latest_adx:.1f} < 20), трендовий вхід заблоковано", scan_logs)
                return None
            elif strategy_type in mean_reversion_strategies and latest_adx > 25:
                log_skip(f"⛔ {symbol} {timeframe} {direction} — сильний тренд (ADX={latest_adx:.1f} > 25), контртрендовий вхід заблоковано", scan_logs)
                return None
        except Exception as e:
            print(f"Помилка розрахунку фільтра ринкового режиму для {symbol}: {e}")

    # 2. Фільтр тренду BTC та розрахунок лінійної кореляції Пірсона (передаємо кешований btc_df)
    btc_pass, correlation = await check_btc_and_correlation(symbol, timeframe, df, direction, scan_logs, btc_df)
    if not btc_pass:
        return None

    # Перевірка тренду на старшому таймфреймі
    bias_ok = await check_higher_tf_bias(symbol, timeframe, direction, scan_logs)
    if not bias_ok:
        return None

    entry = round(price, 6)

    # Отримуємо параметри поточної стратегії
    ema_fast, ema_slow, rsi_min, rsi_max, strategy_type = get_params(symbol, timeframe, direction)
    stop_mult = get_setting('stop_atr_mult')
    tp1_mult = get_setting('tp1_atr_mult')

    if direction == 'SHORT':
        dobar_low = round(entry + atr * 0.5, 6)
        dobar_high = round(entry + atr * 1.5, 6)
        stop_loss = round(entry + atr * stop_mult, 6)
        tps = [
            (round(entry - atr * tp1_mult, 6), 90, round(atr * tp1_mult / entry * 100, 1)),
            (round(entry - atr * (tp1_mult + 0.5), 6), 75, round(atr * (tp1_mult + 0.5) / entry * 100, 1)),
            (round(entry - atr * (tp1_mult + 1.2), 6), 58, round(atr * (tp1_mult + 1.2) / entry * 100, 1)),
            (round(entry - atr * (tp1_mult + 2.2), 6), 40, round(atr * (tp1_mult + 2.2) / entry * 100, 1)),
        ]
    else:
        dobar_low = round(entry - atr * 1.5, 6)
        dobar_high = round(entry - atr * 0.5, 6)
        stop_loss = round(entry - atr * stop_mult, 6)
        tps = [
            (round(entry + atr * tp1_mult, 6), 90, round(atr * tp1_mult / entry * 100, 1)),
            (round(entry + atr * (tp1_mult + 0.5), 6), 75, round(atr * (tp1_mult + 0.5) / entry * 100, 1)),
            (round(entry + atr * (tp1_mult + 1.2), 6), 58, round(atr * (tp1_mult + 1.2) / entry * 100, 1)),
            (round(entry + atr * (tp1_mult + 2.2), 6), 40, round(atr * (tp1_mult + 2.2) / entry * 100, 1)),
        ]

    # Визначаємо мінімальну кількість угод для бектесту
    min_trades = 12 if timeframe in ['5m', '15m', '30m'] else 20

    # Швидкий бектест у фоновому потоці
    stats = await asyncio.to_thread(
        run_backtest,
        df, direction,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        strategy_type=strategy_type,
        stop_mult=stop_mult,
        tp1_mult=tp1_mult,
        min_trades=min_trades,
        min_prob=get_setting('min_tp1_prob')
    )

    if not stats or not stats.get('is_valid', False):
        log_skip(f"⛔ {symbol} {timeframe} {direction} — слабка стратегія на бектесті, пропускаємо", scan_logs)
        return None

    min_prob = get_setting('min_tp1_prob')
    tp1_prob = stats['tp_probs'][0] if stats['count'] > 0 else 0

    if tp1_prob < min_prob:
        log_skip(f"⛔ {symbol} {timeframe} {direction} — ймовірність TP1 ({tp1_prob}%) менша за мінімальну ({min_prob}%), пропускаємо", scan_logs)
        return None

    if stats['count'] > 0:
        tps_with_probs = []
        for i, (tp_price, _, pct) in enumerate(tps):
            prob = stats['tp_probs'][i] if i < len(stats['tp_probs']) else 40
            tps_with_probs.append((tp_price, prob, pct))
        tps = tps_with_probs

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
        'correlation': correlation  # Передаємо розраховану кореляцію до BTC у main.py
    }


async def scan_all(timeframes=None, scan_logs=None):
    if timeframes is None:
        timeframes = get_setting('active_timeframes')

    watchlist = get_setting('watchlist')

    # 1. ГЛОБАЛЬНИЙ КЕШ: Завантажуємо свічки BTC/USDT один раз для кожного таймфрейму
    # Це економить до 90% мережевого трафіку та запобігає бану лімітів API
    btc_candles_map = {}
    for tf in timeframes:
        try:
            limit = 6000 if tf == '5m' else (4000 if tf == '15m' else 1000)
            btc_df = await get_candles('BTC/USDT', tf, limit=limit)
            if btc_df is not None and len(btc_df) >= 50:
                btc_candles_map[tf] = btc_df
        except Exception as e:
            print(f"Помилка завантаження глобального кешу BTC для {tf}: {e}")

    # Семафор обмежує кількість ОДНОЧАСНИХ асинхронних завдань сканування (наприклад, максимум 3)
    # Це захищає Render від вичерпання RAM (OOM) та запобігає бану лімітів API (HTTP 429)
    sem = asyncio.Semaphore(3)

    async def sem_find_signal(symbol, timeframe):
        async with sem:
            # Мікро-пауза між запусками для згладжування навантаження на API
            await asyncio.sleep(0.2)
            btc_df = btc_candles_map.get(timeframe)
            return await find_signal(symbol, timeframe, scan_logs=scan_logs, btc_df=btc_df)

    tasks = [
        sem_find_signal(symbol, timeframe)
        for symbol in watchlist
        for timeframe in timeframes
    ]

    results = await asyncio.gather(
        *tasks,
        return_exceptions=True
    )

    signals = []

    for result in results:
        if isinstance(result, Exception):
            print(f"Scan error: {result}")
            continue

        if result:
            signals.append(result)

    return signals