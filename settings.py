# settings.py
import json
import os

SETTINGS_FILE = 'settings.json'

# ЄДИНЕ ДЖЕРЕЛО ПРАВДИ: Повний список ТОП-100 альткоїнів з 0% комісій на MEXC
ALL_PAIRS = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT',
    'ADA/USDT', 'DOGE/USDT', 'SHIB/USDT', 'LTC/USDT', 'AVAX/USDT',
    'DOT/USDT', 'LINK/USDT', 'UNI/USDT', 'ATOM/USDT', 'NEAR/USDT',
    'TON/USDT', 'SUI/USDT', 'PEPE/USDT', 'WIF/USDT', 'OP/USDT',
    'JUP/USDT', 'POL/USDT', 'RENDER/USDT', 'GRT/USDT', 'AAVE/USDT',
    'INJ/USDT', 'ZRO/USDT', 'PYTH/USDT', 'PNUT/USDT', 'ORDI/USDT',
    'ONDO/USDT', 'ZEC/USDT', 'BCH/USDT', 'ICP/USDT', 'WLD/USDT',
    'XMR/USDT', 'XLM/USDT'
]

DEFAULT_SETTINGS = {
    'active_timeframe': 'all',
    'watchlist': ALL_PAIRS.copy(),
    'active_timeframes': ['5m', '15m', '30m', '1h', '4h', '1d'],
    'stop_atr_mult': 2.0,      
    'tp1_atr_mult': 0.8,       
    'min_tp1_prob': 55,        
    'htf_bias_enabled': True,  
    'htf_diff_threshold': 1.0, 
    'max_active_signals': 10,  
    
    'exchange_name': 'binance', 
    'portfolio_size': 1000.0,   
    'risk_pct': 1.0,            
    'leverage': 20,             
    'use_dobar': True,          
    
    'btc_filter_enabled': True,     
    'regime_filter_enabled': True,   
    
    'funding_filter_enabled': True,  
    'funding_max_limit': 0.05,       
    
    'oi_filter_enabled': True,       
    'oi_min_limit': 10.0,            
    
    'scalper_mode_enabled': True,     
    
    'testnet_enabled': True,         
    'trading_enabled': True,

    # Параметри глобальних запобіжників (Portfolio Circuit Breakers)
    'max_portfolio_margin_pct': 50.0,    
    'max_daily_loss_pct': 3.0,          
    'consecutive_losses_limit': 3,       
    'cooldown_hours': 12                
}

# Глобальний in-memory кеш для усунення дискового блокування I/O
_settings_cache = None


def load_settings():
    """Завантажує налаштування з RAM-кешу або зчитує з диска один раз"""
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache
        
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                _settings_cache = json.load(f)
                return _settings_cache
        except Exception as e:
            print(f"Помилка завантаження налаштувань з диска: {e}")
            
    _settings_cache = DEFAULT_SETTINGS.copy()
    return _settings_cache


def save_settings(settings):
    """Зберігає налаштування на диск та оновлює оперативний кеш"""
    global _settings_cache
    _settings_cache = settings.copy()
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Помилка збереження налаштувань: {e}")


def get_setting(key):
    """Швидке зчитування налаштувань без дискового I/O"""
    settings = load_settings()
    return settings.get(key, DEFAULT_SETTINGS.get(key))


def set_setting(key, value):
    """Швидке оновлення налаштувань з рефрешем кешу"""
    settings = load_settings()
    settings[key] = value
    save_settings(settings)


def to_native_float(val):
    if val is None:
        return None
    try:
        return float(val)
    except Exception:
        return val


def to_native_int(val):
    if val is None:
        return None
    try:
        return int(val)
    except Exception:
        return val


def get_exchange_client(async_mode=False):
    import ccxt
    if async_mode:
        import ccxt.async_support as ccxt_async
        lib = ccxt_async
    else:
        lib = ccxt
        
    exchange_name = get_setting('exchange_name') or 'binance'
    
    if exchange_name == 'mexc':
        return lib.mexc({
            'enableRateLimit': True,
            'verify': False,
            'options': {'defaultType': 'swap'},  
            'aiohttp_trust_env': False
        })
    else:
        return lib.binanceusdm({
            'enableRateLimit': True,
            'verify': False,
            'aiohttp_trust_env': False
        })