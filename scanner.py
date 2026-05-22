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
        # Безпечне подвійне блокування (Double-Checked Locking)
        # Навіть якщо 100 тасок одночасно викличуть цю функцію,
        # запит на Binance піде лише ОДИН РАЗ від першої таски.
        if not exchange.markets:
            async with _load_markets_lock:
                if not exchange.markets:
                    print("⏳ Попереднє завантаження інформації про ринки Binance...")
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
        if direction == 'SHORT':
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


async def check_higher_tf_bias(symbol, timeframe, direction):
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
            return False

        if direction == 'LONG':
            return ema20 > ema50
        elif direction == 'SHORT':
            return ema20 < ema50

        return True

    except Exception as e:
        print(f"HTF bias error {symbol} {timeframe}: {e}")
        return True

async def find_signal(symbol, timeframe):
    # Динамічно збільшуємо ліміт свічок для дрібних таймфреймів
    limit = 1000
    if timeframe == '5m':
        limit = 6000   # ~20 днів історії для 5m, щоб назбирати угоди для бектесту
    elif timeframe == '15m':
        limit = 4000   # ~41 день історії для 15m

    df = await get_candles(symbol, timeframe, limit=limit)
    if df is None or len(df) < 60:
        return None

# Виконуємо розрахунок індикаторів у фоновому потоці, не блокуючи Event Loop
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

    # Перевірка тренду на старшому таймфреймі
    bias_ok = await check_higher_tf_bias(symbol, timeframe, direction)
    if not bias_ok:
        return None

    entry = round(price, 6)

    # Отримуємо параметри поточної стратегії для правильного бектесту та розрахунку цілей
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

# Виконуємо бектест у фоновому потоці, повністю вивільняючи ресурси бота
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
        min_trades=min_trades
    )

    if not stats or not stats.get('is_valid', False):
        print(f"⛔ {symbol} {timeframe} {direction} — слабка стратегія на бектесті, пропускаємо")
        return None

    min_prob = get_setting('min_tp1_prob')
    tp1_prob = stats['tp_probs'][0] if stats['count'] > 0 else 0

    if tp1_prob < min_prob:
        print(f"⛔ {symbol} {timeframe} {direction} — ймовірність TP1 ({tp1_prob}%) менша за мінімальну ({min_prob}%), пропускаємо")
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
    }

async def scan_all(timeframes=None):
    if timeframes is None:
        timeframes = get_setting('active_timeframes')

    watchlist = get_setting('watchlist')

    # Тут прибрано попередній load_markets, оскільки тепер 
    # безпечне подвійне блокування (Double-Checked Locking) 
    # інтегровано безпосередньо всередину функції get_candles.

    tasks = [
        find_signal(symbol, timeframe)
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