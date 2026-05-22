import json
import os

SETTINGS_FILE = 'settings.json'

DEFAULT_SETTINGS = {
    'active_timeframe': 'all',
    'watchlist': [
        'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT',
        'XRP/USDT', 'DOGE/USDT', 'ADA/USDT', 'AVAX/USDT',
        'DOT/USDT', 'POL/USDT', 'LINK/USDT', 'UNI/USDT',
        'ATOM/USDT', 'LTC/USDT', 'ETC/USDT', 'FIL/USDT',
    ],
    'active_timeframes': ['5m', '15m', '30m', '1h', '4h', '1d'],
    'stop_atr_mult': 2.0,      # множник ATR для стопу
    'tp1_atr_mult': 0.8,       # множник ATR для TP1
    'min_tp1_prob': 55,        # мінімальна ймовірність TP1 для сигналу
    'htf_bias_enabled': True,  # фільтр вищого таймфрейму
    'htf_diff_threshold': 1.0, # поріг різниці EMA для HTF фільтру (%)
    'max_active_signals': 10,  # максимум активних сигналів одночасно
    
    # Нові поля для ризику та вибору біржі
    'exchange_name': 'binance', # поточна активна біржа: 'binance' або 'mexc'
    'portfolio_size': 1000.0,   # загальний розмір депозиту в USD
    'risk_pct': 1.0             # ризик на одну угоду у відсотках від депо (1%)
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Помилка завантаження налаштувань: {e}")
    return DEFAULT_SETTINGS.copy()

def save_settings(settings):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Помилка збереження налаштувань: {e}")

def get_setting(key):
    settings = load_settings()
    return settings.get(key, DEFAULT_SETTINGS.get(key))

def set_setting(key, value):
    settings = load_settings()
    settings[key] = value
    save_settings(settings)

def get_exchange_client(async_mode=False):
    """Генерує динамічний клієнт підключення для Binance Futures або MEXC Futures"""
    import ccxt
    if async_mode:
        import ccxt.async_support as ccxt_async
        lib = ccxt_async
    else:
        lib = ccxt
        
    exchange_name = get_setting('exchange_name') or 'binance'
    
    if exchange_name == 'mexc':
        # Налаштування для безшовного переходу на безстрокові ф'ючерси (Swap) на MEXC
        return lib.mexc({
            'enableRateLimit': True,
            'verify': False,
            'options': {'defaultType': 'swap'},  # Автоматично підміняє спотові запити на ф'ючерсні
            'aiohttp_trust_env': False
        })
    else:
        # Налаштування для ф'ючерсів USDT-M на Binance
        return lib.binanceusdm({
            'enableRateLimit': True,
            'verify': False,
            'aiohttp_trust_env': False
        })