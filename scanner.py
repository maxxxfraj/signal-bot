# scanner.py
import ccxt.async_support as ccxt
import pandas as pd
import ta
import os
import json
import asyncio
from backtest import run_backtest
from settings import get_setting, get_exchange_client, to_native_float, to_native_int
from regime_classifier import MarketRegimeClassifier
from database import load_strategy_config_from_db

# Ініціалізація динамічного асинхронного клієнта біржі (Binance або MEXC)
exchange = get_exchange_client(async_mode=True)

_load_markets_lock = asyncio.Lock()

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

def get_params(symbol, timeframe, direction):
    symbol_clean = symbol.replace('/', '')
    
    # Завантажуємо параметри безпосередньо з хмари Neon PostgreSQL
    db_params = load_strategy_config_from_db(symbol_clean, timeframe, direction)
    
    if db_params:
        return (
            db_params['ema_fast'],
            db_params['ema_slow'],
            db_params['rsi_min'],
            db_params['rsi_max'],
            db_params['strategy_type']
        )
    else:
        # Фолбек на безпечні значення за замовчуванням, якщо монета ще не була оптимізована
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
    df['ema20'] = ta.trend.ema_indicator(df['close'], window=20)
    df['ema50'] = ta.trend.ema_indicator(df['close'], window=50)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)

    ap = (df['high'] + df['low'] + df['close']) / 3.0
    esa = ta.trend.ema_indicator(ap, window=10)
    d = ta.trend.ema_indicator(abs(ap - esa), window=10)
    
    d_val = d.copy()
    d_val[d_val == 0] = 0.000001
    
    ci = (ap - esa) / (0.015 * d_val)
    df['wt1'] = ta.trend.ema_indicator(ci, window=21)
    df['wt2'] = df['wt1'].rolling(window=4).mean()

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

    elif strategy_type == 'wavetrend_bounce':
        dot_level = 45
        wt1_last = df['wt1'].iloc[-2]
        wt2_last = df['wt2'].iloc[-2]
        wt1_prev = df['wt1'].iloc[-3]
        wt2_prev = df['wt2'].iloc[-3]
        
        if direction == 'SHORT':
            return (wt1_prev > wt2_prev and 
                    last['wt1'] < last['wt2'] and 
                    last['wt1'] > dot_level)
        else:
            return (wt1_prev < wt2_prev and 
                    last['wt1'] > last['wt2'] and 
                    last['wt1'] < -dot_level)

    elif strategy_type == 'breakout':
        high_20 = df['high'].rolling(20).max().iloc[-2]
        low_20 = df['low'].rolling(20).min().iloc[-2]
        if direction == 'LONG':
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
    btc_filt = get_setting('btc_filter_enabled')
    if btc_filt is None:
        btc_filt = True
        
    try:
        df_btc = btc_df
        if df_btc is None:
            df_btc = await get_candles('BTC/USDT', timeframe, limit=len(df_alt))
            
        if df_btc is None or len(df_btc) < 50:
            return True, 0.0

        df_alt_aligned, df_btc_aligned = df_alt.align(df_btc, join='inner', axis=0)
        returns_alt = df_alt_aligned['close'].pct_change().dropna()
        returns_btc = df_btc_aligned['close'].pct_change().dropna()
        
        correlation = returns_alt.corr(returns_btc)
        if pd.isna(correlation):
            correlation = 0.0

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
    open_interest = None
    funding_rate = None

    # Дефолтні значення для зворотної сумісності та розрахунку ризику
    regime = "STABLE_TREND"
    er = 0.50
    z_vol = 0.0

    scalp_enabled = get_setting('scalper_mode_enabled')
    if scalp_enabled is None:
        scalp_enabled = True
        
    if not scalp_enabled and timeframe in ['5m', '15m', '30m', '1h']:
        return None

    exchange_name = get_setting('exchange_name') or 'binance'
    limit = 1000
    
    if exchange_name == 'binance':
        if timeframe == '5m':
            limit = 6000
        elif timeframe == '15m':
            limit = 4000
    else:
        limit = 1000

    df = await get_candles(symbol, timeframe, limit=limit)
    if df is None or len(df) < 60:
        return None

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

    ema_fast, ema_slow, rsi_min, rsi_max, strategy_type = get_params(symbol, timeframe, direction)

    is_scalp_strategy = strategy_type in ['wavetrend_bounce', 'bb_bounce', 'mean_reversion']
    is_scalp_tf = timeframe in ['5m', '15m', '30m', '1h']
    mode = 'scalp' if (is_scalp_strategy or is_scalp_tf) else 'swing'

    # --- ІНТЕГРАЦІЯ MARKET REGIME CLASSIFIER ---
    regime_filter = get_setting('regime_filter_enabled')
    if regime_filter is None:
        regime_filter = True
        
    if regime_filter:
        try:
            # Використовуємо новий стабільний класифікатор
            classifier = MarketRegimeClassifier()
            classification = classifier.classify(df)
            regime = classification["regime"]
            er = classification["er"]
            z_vol = classification["z_vol"]
            
            trend_strategies = ['ema_rsi', 'macd_cross', 'breakout', 'vol_spike']
            mean_reversion_strategies = ['bb_bounce', 'mean_reversion', 'wavetrend_bounce']
            
            # Фільтрація з урахуванням виявленого фазового стану ринку
            if strategy_type in trend_strategies and regime in ["LOW_VOL_FLAT", "MEAN_REVERSION"]:
                log_skip(f"⛔ {symbol} {timeframe} {direction} — ринок у ренджі ({regime}), трендовий вхід заблоковано класифікатором", scan_logs)
                return None
            elif strategy_type in mean_reversion_strategies and regime in ["STABLE_TREND"]:
                log_skip(f"⛔ {symbol} {timeframe} {direction} — сильний тренд ({regime}), контртрендовий вхід заблоковано класифікатором", scan_logs)
                return None
            elif regime == "HIGH_VOL_CHAOS":
                log_skip(f"⛔ {symbol} {timeframe} {direction} — ринок у фазі аномальної волатильності/хаосу ({regime}), торгівлю зупинено", scan_logs)
                return None
        except Exception as e:
            print(f"Помилка розрахунку фільтра ринкового режиму для {symbol}: {e}")
    # ---------------------------------------------

    btc_pass, correlation = await check_btc_and_correlation(symbol, timeframe, df, direction, scan_logs, btc_df)
    if not btc_pass:
        return None

    bias_ok = await check_higher_tf_bias(symbol, timeframe, direction, scan_logs)
    if not bias_ok:
        return None

    entry = round(price, 6)

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

    exchange_name = get_setting('exchange_name') or 'binance'
    if exchange_name == 'mexc':
        min_trades = 6 if timeframe in ['5m', '15m', '30m'] else 10
    else:
        min_trades = 12 if timeframe in ['5m', '15m', '30m'] else 20

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

    ccxt_symbol = f"{symbol}:USDT"
    
    funding_filt = get_setting('funding_filter_enabled')
    if funding_filt is None:
        funding_filt = True
    funding_max_pct = get_setting('funding_max_limit') or 0.05
    
    try:
        funding_data = await exchange.fetch_funding_rate(ccxt_symbol)
        funding_rate = funding_data.get('fundingRate')
    except ccxt.BadSymbol:
        try:
            ccxt_symbol_fallback = symbol
            funding_data = await exchange.fetch_funding_rate(ccxt_symbol_fallback)
            funding_rate = funding_data.get('fundingRate')
        except Exception as e:
            print(f"Помилка отримання Funding Rate (fallback) для {symbol}: {e}")
    except Exception as e:
        print(f"Помилка отримання Funding Rate для {symbol}: {e}")
        
    if funding_rate is not None and funding_filt:
        funding_rate_pct = funding_rate * 100.0
        if direction == 'LONG' and funding_rate_pct >= funding_max_pct:
            log_skip(f"⛔ {symbol} — Фільтр фандингу заблокував LONG (Перегрів покупців: {funding_rate_pct:.3f}% >= {funding_max_pct}%)", scan_logs)
            return None
        elif direction == 'SHORT' and funding_rate_pct <= -funding_max_pct:
            log_skip(f"⛔ {symbol} — Фільтр фандингу заблокував SHORT (Перегрів продавців: {funding_rate_pct:.3f}% <= -{funding_max_pct}%)", scan_logs)
            return None
        
    try:
        ccxt_symbol = f"{symbol}:USDT"
        oi_data = await exchange.fetch_open_interest(ccxt_symbol)
        oi_val = to_native_float(oi_data.get('openInterestValue'))
        oi_amount = to_native_float(oi_data.get('openInterestAmount'))
        
        if oi_val is not None:
            open_interest = oi_val
        elif oi_amount is not None:
            open_interest = oi_amount * price
        else:
            open_interest = None
            
    except ccxt.BadSymbol:
        try:
            ccxt_symbol_fallback = symbol
            oi_data = await exchange.fetch_open_interest(ccxt_symbol_fallback)
            oi_val = to_native_float(oi_data.get('openInterestValue'))
            oi_amount = to_native_float(oi_data.get('openInterestAmount'))
            
            if oi_val is not None:
                open_interest = oi_val
            elif oi_amount is not None:
                open_interest = oi_amount * price
            else:
                open_interest = None
        except Exception as e:
            open_interest = None
    except Exception as e:
        open_interest = None

    oi_filt = get_setting('oi_filter_enabled')
    if oi_filt is None:
        oi_filt = True
    oi_min_limit = get_setting('oi_min_limit') or 10.0
    
    if open_interest is not None and oi_filt:
        oi_m = open_interest / 1000000.0
        if oi_m < oi_min_limit:
            log_skip(f"⛔ {symbol} — Фільтр мінімального OI заблокував сигнал (Низька деривативна ліквідність: ${oi_m:.2f}M < ${oi_min_limit:.1f}M)", scan_logs)
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
        'correlation': correlation,
        'funding_rate': funding_rate * 100.0 if funding_rate is not None else None,
        'open_interest': open_interest,
        'mode': mode,
        'strategy_type': strategy_type, # Передаємо тип стратегії
        'regime': regime,               # Передаємо фазу
        'er': er,                       # Передаємо Kaufman ER
        'z_vol': z_vol                  # Передаємо Z-Score волатильності
    }


async def scan_all(timeframes=None, scan_logs=None):
    if timeframes is None:
        timeframes = get_setting('active_timeframes')

    watchlist = get_setting('watchlist')

    btc_candles_map = {}
    for tf in timeframes:
        try:
            limit = 6000 if tf == '5m' else (4000 if tf == '15m' else 1000)
            btc_df = await get_candles('BTC/USDT', tf, limit=limit)
            if btc_df is not None and len(btc_df) >= 50:
                btc_candles_map[tf] = btc_df
        except Exception as e:
            print(f"Помилка завантаження глобального кешу BTC для {tf}: {e}")

    sem = asyncio.Semaphore(3)

    async def sem_find_signal(symbol, timeframe):
        async with sem:
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